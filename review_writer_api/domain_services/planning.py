"""PostgreSQL-native Matrix, outline, and Blueprint workflows."""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.credentials import (
    ProviderKind,
    ProviderSettingsError,
    ProviderSettingsService,
)
from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.scientific_runner import (
    SENSITIVE_ENVIRONMENT_KEY,
    ScientificRunner,
)
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.taxonomy import TaxonomyConfigurationError, load_taxonomy_rules
from review_writer_core.review_structure import (
    assign_primary_paper_sections,
    infer_section_role,
)


MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
OUTLINE_LOGICAL_NAME = "planning/selected_outline.json"
REFERENCE_INDEX_LOGICAL_NAME = "planning/reference_outlines.json"
BLUEPRINT_LOGICAL_NAME = "blueprint/section_blueprint.json"
DISCOVERY_LOGICAL_NAME = "discovery/review.json"

OUTLINE_STYLES: dict[str, dict[str, str]] = {
    "substrate": {
        "en": "Substrate-classified",
        "zh": "按底物分类",
        "axis": "substrate classes and scope",
        "tag_key": "substrate",
        "introduction": "define the review scope and explain why substrate class is the primary comparison axis",
    },
    "catalyst": {
        "en": "Catalyst and method-classified",
        "zh": "按催化剂与方法分类",
        "axis": "catalysts, methods, and operating principles",
        "tag_key": "catalyst_or_method",
        "introduction": "compare how catalysts or methods shape outcomes, evidence quality, and applicability",
    },
    "reaction": {
        "en": "Reaction-type-classified",
        "zh": "按反应类型分类",
        "axis": "transformation and mechanistic strategy",
        "tag_key": "reaction_type",
        "introduction": "organize the literature by transformation logic and mechanistic strategy",
    },
}


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _paper_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("paper_id")) for row in rows if str(row.get("paper_id") or "").strip()]


def _outline_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in str(markdown or "").replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^##\s+(?:\d+[.)]\s*)?(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip()
            current = {
                "title": title,
                "paper_ids": [],
                "section_role": infer_section_role(title),
            }
            sections.append(current)
            continue
        if current is not None and line.casefold().startswith("section role:"):
            role = line.split(":", 1)[1].strip().casefold()
            if role in {"introduction", "body", "conclusion", "references"}:
                current["section_role"] = role
            continue
        if current is not None and line.casefold().startswith("assigned papers:"):
            assigned = line.split(":", 1)[1].strip().rstrip(".。")
            current["paper_ids"] = list(
                dict.fromkeys(
                    paper_id.strip()
                    for paper_id in re.split(r"[,，;；]", assigned)
                    if paper_id.strip()
                )
            )
    return sections


class PlanningService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        *,
        scientific_runner: ScientificRunner | None = None,
        provider_settings: ProviderSettingsService | None = None,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.scientific_runner = scientific_runner
        self.provider_settings = provider_settings
        self.root = Path(__file__).resolve().parents[2]
        self._write_lock = threading.RLock()

    def _owned_project(self, principal: Principal, project_id: str):
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

    @staticmethod
    def _reference_candidate_is_isolated(candidate: Any) -> bool:
        if not isinstance(candidate, dict):
            return False
        firewall = candidate.get("content_firewall")
        return bool(
            candidate.get("analysis_mode") == "ai_style_only_transfer_v2"
            and candidate.get("content_source") == "current_matrix_only"
            and candidate.get("reference_content_reused") is False
            and isinstance(firewall, dict)
            and firewall.get("transfer_received_reference_text") is False
            and firewall.get("all_heading_levels_content_source")
            == "current_matrix_only"
        )

    def _read_json(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[dict[str, Any] | None, ArtifactRecord | None]:
        self._owned_project(principal, project_id)
        artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )
        if artifact is None:
            if required:
                raise WorkflowNotFound("Planning artifact not found.")
            return None, None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict("The current Planning artifact is unreadable.") from exc
        if not isinstance(payload, dict):
            raise WorkflowConflict("The current Planning artifact is invalid.")
        return payload, artifact

    def _publish_files(
        self,
        principal: Principal,
        project_id: str,
        *,
        stage_id: str,
        files: dict[str, tuple[bytes, str]],
        input_snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            stage_id,
            status="succeeded",
            input_snapshot=input_snapshot or {},
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        published: dict[str, ArtifactRecord] = {}
        for index, (logical_name, (content, artifact_type)) in enumerate(files.items()):
            filename = f"{index:03d}-{Path(logical_name).name}"
            (staging / filename).write_bytes(content)
            published[logical_name] = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                filename,
                logical_name=logical_name,
                artifact_type=artifact_type,
                producer_stage=stage_id,
                make_current=False,
            )
        return published, run

    def _matrix(self, principal: Principal, project_id: str):
        matrix, artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        rows = matrix.get("rows") if isinstance(matrix, dict) else None
        if not isinstance(rows, list) or not rows:
            raise WorkflowConflict(
                "No literature matrix is available. Confirm Discovery first."
            )
        return matrix, artifact

    @staticmethod
    def _tag_value(value: Any) -> str:
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        if isinstance(value, (list, tuple)):
            value = next(
                (
                    item
                    for item in value
                    if str(item or "").strip()
                    and str(item).strip().casefold()
                    not in {"not specified", "none", "unknown"}
                ),
                "",
            )
        normalized = str(value or "").strip()
        return (
            ""
            if normalized.casefold()
            in {"not specified", "none", "unknown", "n/a"}
            else normalized
        )

    def _outline_sources(
        self,
        principal: Principal,
        rows: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        paper_ids = _paper_ids(rows)
        if not paper_ids:
            return {}, {}
        with database_session(self.repository.session_factory) as session:
            records = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(tuple(paper_ids)),
                    LibraryPaper.status == "active",
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
        tags_by_paper: dict[str, dict[str, Any]] = {}
        text_by_paper: dict[str, str] = {}
        rows_by_id = {str(row.get("paper_id") or ""): row for row in rows}
        for record in records:
            metadata = (
                record.metadata_json if isinstance(record.metadata_json, dict) else {}
            )
            structured = metadata.get("structured_tags")
            if isinstance(structured, dict) and "value" in structured:
                structured = structured.get("value")
            tags = dict(structured) if isinstance(structured, dict) else {}
            if isinstance(record.tags_json, dict):
                tags.update(record.tags_json)
            row = rows_by_id.get(record.paper_id) or {}
            # Project Tags are scoped to the active project. Explicit legacy
            # confirmations and the current automatic Discovery assessment
            # both take precedence over reusable Library metadata without
            # mutating the Library record.
            project_tags = row.get("project_tags")
            if (
                row.get("project_tag_review_status") in {"confirmed", "automatic"}
                and isinstance(project_tags, dict)
            ):
                tags.update(project_tags)
            tags_by_paper[record.paper_id] = tags
            parts = [
                row.get("title"),
                row.get("abstract"),
                row.get("main_content"),
                " ".join(str(item) for item in (row.get("keywords") or [])),
                record.title,
                " ".join(str(item) for item in (record.keywords_json or [])),
            ]
            text_by_paper[record.paper_id] = " ".join(
                str(part) for part in parts if str(part or "").strip()
            ).casefold()
        return tags_by_paper, text_by_paper

    @staticmethod
    def _semantic_outline_groups(
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        try:
            rules = [
                (label, aliases)
                for label, category, aliases in load_taxonomy_rules(
                    Path.cwd(), profile=taxonomy_profile
                )
                if category == tag_key
            ]
        except TaxonomyConfigurationError:
            return {}
        groups: dict[str, list[str]] = {}
        other: list[str] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            text = text_by_paper.get(paper_id, "")
            ranked: list[tuple[int, int, str]] = []
            for index, (label, aliases) in enumerate(rules):
                score = 0
                for term in (label, *aliases):
                    normalized = str(term or "").strip().casefold()
                    if not normalized:
                        continue
                    pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
                    if re.search(pattern, text):
                        score = max(
                            score,
                            len(normalized.split()) * 10 + len(normalized),
                        )
                ranked.append((score, -index, label))
            best = max(ranked, default=(0, 0, ""))
            if best[0] > 0:
                groups.setdefault(best[2], []).append(paper_id)
            else:
                other.append(paper_id)
        if other:
            groups["Other or unspecified"] = other
        return groups

    def _outline_groups(
        self,
        rows: list[dict[str, Any]],
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        other: list[str] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            label = self._tag_value(
                (tags_by_paper.get(paper_id) or {}).get(tag_key)
            )
            if label:
                groups.setdefault(label, []).append(paper_id)
            else:
                other.append(paper_id)
        if other:
            groups["Other or unspecified"] = other
        meaningful = {
            label: paper_ids
            for label, paper_ids in groups.items()
            if label != "Other or unspecified" and paper_ids
        }
        largest_share = max(
            (len(paper_ids) for paper_ids in meaningful.values()), default=0
        ) / max(1, len(rows))
        if len(rows) < 6 or (len(meaningful) >= 2 and largest_share < 0.85):
            return groups
        semantic = self._semantic_outline_groups(
            rows,
            text_by_paper,
            tag_key=tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        semantic_meaningful = [
            paper_ids
            for label, paper_ids in semantic.items()
            if label != "Other or unspecified" and paper_ids
        ]
        return semantic if len(semantic_meaningful) >= 2 else groups

    def _outline_document(
        self,
        style: str,
        rows: list[dict[str, Any]],
        *,
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        taxonomy_profile: str,
    ) -> str:
        definition = OUTLINE_STYLES[style]
        groups = self._outline_groups(
            rows,
            tags_by_paper,
            text_by_paper,
            tag_key=definition["tag_key"],
            taxonomy_profile=taxonomy_profile,
        )
        lines = [
            "# Selected Outline",
            "",
            f"Primary structure: {definition['en']}.",
            "This working outline remains fully editable before Blueprint generation.",
            "",
            "## Introduction",
            "Section role: introduction",
            f"Purpose: {definition['introduction']}.",
            "",
        ]
        for index, (label, paper_ids) in enumerate(groups.items(), start=1):
            lines.extend(
                [
                    f"## {index}. {label}",
                    "Section role: body",
                    f"Assigned papers: {', '.join(paper_ids)}.",
                    f"Purpose: compare the selected papers within this {definition['axis']} category.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Cross-category comparison and conclusion",
                "Section role: conclusion",
                "Purpose: compare the main systems, outcomes, evidence boundaries, limitations, and future directions.",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _validate_outline(markdown: str, matrix_ids: set[str]) -> str:
        text = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise WorkflowValidationError("Outline Markdown must not be empty.")
        if len(text) > 250_000:
            raise WorkflowValidationError("Outline Markdown exceeds 250,000 characters.")
        sections = _outline_sections(text)
        if not sections:
            raise WorkflowValidationError(
                "Outline Markdown needs at least one level-2 heading (##)."
            )
        missing = [
            section["title"]
            for section in sections
            if not section["paper_ids"]
            and section.get("section_role")
            not in {"introduction", "conclusion", "references"}
        ]
        if missing:
            raise WorkflowValidationError(
                "Every major section must assign at least one paper.",
                details={"sections": missing},
            )
        unknown = sorted(
            {
                paper_id
                for section in sections
                for paper_id in section["paper_ids"]
                if paper_id not in matrix_ids
            }
        )
        if unknown:
            raise WorkflowValidationError(
                "Outline paper assignments must resolve to the current Matrix.",
                details={"paper_ids": unknown},
            )
        return text + "\n"

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        matrix, matrix_artifact = self._matrix(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME, required=False
        )
        references, _references_artifact = self._read_json(
            principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
        )
        blueprint, blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        blueprint_state = self.repository.get_stage_state(
            principal.user_id, project_id, "blueprint"
        )
        rows = matrix["rows"]
        project = self._owned_project(principal, project_id)
        tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
        selected_ids: list[str] = []
        for group in (discovery or {}).get("results") or []:
            if not isinstance(group, dict) or group.get("keep") is False:
                continue
            for row in group.get("local_results") or []:
                if (
                    isinstance(row, dict)
                    and row.get("selected_for_matrix")
                    and str(row.get("role") or "") != "excluded"
                ):
                    paper_id = str(row.get("paper_id") or "")
                    if paper_id and paper_id not in selected_ids:
                        selected_ids.append(paper_id)
        matrix_sync = dict(matrix.get("sync") or {})
        selection_current = bool(
            selected_ids
            and set(_paper_ids(rows)) == set(selected_ids)
        )
        generated = [
            {
                "candidate_id": style,
                "outline_style": style,
                "labels": {"en": definition["en"], "zh": definition["zh"]},
                "outline_md": self._outline_document(
                    style,
                    rows,
                    tags_by_paper=tags_by_paper,
                    text_by_paper=text_by_paper,
                    taxonomy_profile=project.taxonomy_profile,
                ),
                "source": "builtin",
            }
            for style, definition in OUTLINE_STYLES.items()
        ]
        if outline_artifact is not None and outline is not None:
            generated.append(
                {
                    "candidate_id": "saved-current",
                    "outline_style": outline.get("outline_style", "custom"),
                    "labels": {"en": "Saved outline", "zh": "已保存大纲"},
                    "outline_md": str(outline.get("outline_md") or ""),
                    "source": "saved",
                    "artifact_id": outline_artifact.id,
                }
            )
        all_reference_candidates = list((references or {}).get("candidates") or [])
        reference_candidates = [
            candidate
            for candidate in all_reference_candidates
            if self._reference_candidate_is_isolated(candidate)
        ]
        return {
            "project_id": project_id,
            "topic": str(matrix.get("review_topic") or (discovery or {}).get("topic") or ""),
            "literature_matrix": matrix,
            "matrix_artifact_id": matrix_artifact.id,
            "matrix_revision": matrix_state.revision if matrix_state else 0,
            "matrix_sync": {**matrix_sync, "selection_current": selection_current},
            "discovery_selection": {
                "selected_paper_count": len(selected_ids),
                "selected_paper_ids": selected_ids,
                "selection_current": selection_current,
            },
            "selected_outline_md": str((outline or {}).get("outline_md") or ""),
            "outline_selection": (
                {**outline, "artifact_id": outline_artifact.id}
                if outline is not None and outline_artifact is not None
                else None
            ),
            "outline_candidates": generated + reference_candidates,
            "reference_outline_candidates": reference_candidates,
            "legacy_reference_outline_count": len(all_reference_candidates)
            - len(reference_candidates),
            "section_blueprint": blueprint,
            "blueprint_artifact_id": blueprint_artifact.id if blueprint_artifact else None,
            "blueprint_revision": blueprint_state.revision if blueprint_state else 0,
            "section_writing_plan_md": str(
                (blueprint or {}).get("section_writing_plan_md") or ""
            ),
            "workspace": {
                "active_stage": "planning",
                "tabs": [
                    {
                        "id": "matrix",
                        "labels": {"en": "Literature Matrix", "zh": "文献矩阵"},
                    },
                    {
                        "id": "blueprint",
                        "labels": {"en": "Blueprint", "zh": "章节蓝图"},
                    },
                ],
            },
        }

    def update_matrix_row(
        self,
        principal: Principal,
        project_id: str,
        paper_id: str,
        *,
        revision: int,
        main_content: str | None,
        most_relevant_figure: dict[str, Any] | None,
        mark_complete: bool,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        updated = deepcopy(matrix)
        row = next(
            (
                item
                for item in updated["rows"]
                if isinstance(item, dict) and str(item.get("paper_id")) == paper_id
            ),
            None,
        )
        if row is None:
            raise WorkflowNotFound("Matrix paper was not found.")
        if main_content is not None:
            row["main_content"] = str(main_content).strip()
        if most_relevant_figure is not None:
            row["most_relevant_figure"] = dict(most_relevant_figure)
        if mark_complete and len(re.sub(r"\s+", "", str(row.get("main_content") or ""))) < 300:
            raise WorkflowConflict(
                "Add at least 300 characters of full-paper reading notes before marking this paper complete."
            )
        row["matrix_status"] = (
            "full_reading_complete" if mark_complete else "needs_full_reading"
        )
        updated["updated_at"] = utc_now().isoformat()
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={"paper_id": paper_id},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint",
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "project_id": project_id,
            "paper_id": paper_id,
            "row": row,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
        }

    def save_outline(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        outline_style: str,
        outline_md: str | None,
        manual: bool,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        rows = matrix["rows"]
        project = self._owned_project(principal, project_id)
        matrix_ids = set(_paper_ids(rows))
        style = str(outline_style or "").strip().casefold()
        if style == "custom" and not manual:
            markdown = ""
            complete = False
        elif style.startswith("reference:") and not manual:
            references, _artifact = self._read_json(
                principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
            )
            candidate_id = style.removeprefix("reference:")
            candidate = next(
                (
                    item
                    for item in (references or {}).get("candidates") or []
                    if str(item.get("candidate_id")) == candidate_id
                ),
                None,
            )
            if not isinstance(candidate, dict):
                raise WorkflowNotFound("Reference outline candidate was not found.")
            if not self._reference_candidate_is_isolated(candidate):
                raise WorkflowConflict(
                    "This legacy reference outline did not pass content isolation. Upload the reference again to learn format only."
                )
            markdown = self._validate_outline(
                str(candidate.get("outline_md") or ""), matrix_ids
            )
            complete = True
        elif manual:
            if style != "custom" and style not in OUTLINE_STYLES and not style.startswith("reference:"):
                raise WorkflowValidationError("Unknown outline style.")
            markdown = self._validate_outline(str(outline_md or ""), matrix_ids)
            complete = True
        else:
            if style not in OUTLINE_STYLES:
                raise WorkflowValidationError("Unknown outline style.")
            tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
            markdown = self._outline_document(
                style,
                rows,
                tags_by_paper=tags_by_paper,
                text_by_paper=text_by_paper,
                taxonomy_profile=project.taxonomy_profile,
            )
            complete = True
        payload = {
            "outline_style": style,
            "outline_md": markdown,
            "outline_complete": complete,
            "selection_source": "manual" if manual else "custom_draft" if not complete else "template",
            "manually_edited": bool(manual),
            "source_matrix_artifact_id": matrix_artifact.id,
            "saved_at": utc_now().isoformat(),
        }
        current_outline, current_outline_artifact = self._read_json(
            principal,
            project_id,
            OUTLINE_LOGICAL_NAME,
            required=False,
        )
        current_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if (
            current_outline_artifact is not None
            and isinstance(current_outline, dict)
            and current_state is not None
            and current_state.revision == int(revision)
            and str(current_outline.get("outline_style") or "") == style
            and str(current_outline.get("outline_md") or "") == markdown
            and bool(current_outline.get("outline_complete")) == complete
            and str(current_outline.get("source_matrix_artifact_id") or "")
            == matrix_artifact.id
        ):
            return {
                "project_id": project_id,
                "outline_style": style,
                "selected_outline_md": markdown,
                "outline_complete": complete,
                "blueprint_pending": complete,
                "outline_artifact_id": current_outline_artifact.id,
                "matrix_revision": current_state.revision,
                "unchanged": True,
            }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={OUTLINE_LOGICAL_NAME: (_json_bytes(payload), "json")},
                input_snapshot={"outline_style": style, "manual": manual},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={OUTLINE_LOGICAL_NAME: published[OUTLINE_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint",
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "project_id": project_id,
            "outline_style": style,
            "selected_outline_md": markdown,
            "outline_complete": complete,
            "blueprint_pending": complete,
            "outline_artifact_id": published[OUTLINE_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
        }

    def _analyze_reference_document(
        self,
        principal: Principal,
        project_id: str,
        *,
        candidate_id: str,
        safe_name: str,
        raw: bytes,
        matrix: dict[str, Any],
    ) -> dict[str, Any]:
        if self.scientific_runner is None:
            raise WorkflowValidationError(
                "Reference-format analysis is unavailable in this deployment."
            )
        environment: dict[str, str] = {}
        if self.provider_settings is not None:
            try:
                environment = self.provider_settings.runtime_environment(
                    principal,
                    provider_kinds=(ProviderKind.TEXT,),
                )
            except ProviderSettingsError as exc:
                raise WorkflowValidationError(
                    "Configure and enable the text provider before analyzing a reference review."
                ) from exc
            if not environment.get("OPENAI_API_KEY"):
                raise WorkflowValidationError(
                    "Configure and enable the text provider before analyzing a reference review."
                )
        script = (
            self.root
            / "skills"
            / "review-reference-outline-template"
            / "scripts"
            / "analyze_reference_review.py"
        )
        if not script.is_file():
            raise WorkflowValidationError(
                "The reference-format analysis skill is not installed."
            )
        staging_parent = self.artifacts.workspace_manager.trusted_user_directory(
            principal.user_id,
            ".review-writer",
            "reference-outline-analysis",
        )
        with tempfile.TemporaryDirectory(
            prefix=f"{candidate_id}-", dir=staging_parent
        ) as temporary:
            staging = Path(temporary).resolve()
            source = staging / safe_name
            matrix_path = staging / "literature_matrix.json"
            output_path = staging / "candidate.json"
            source.write_bytes(raw)
            matrix_path.write_text(
                json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            normal_environment = {
                key: value
                for key, value in environment.items()
                if not SENSITIVE_ENVIRONMENT_KEY.search(key)
            }
            secret_environment = {
                key: value
                for key, value in environment.items()
                if SENSITIVE_ENVIRONMENT_KEY.search(key)
            }
            self.scientific_runner.run(
                (
                    sys.executable,
                    str(script),
                    "--input",
                    str(source),
                    "--matrix",
                    str(matrix_path),
                    "--output",
                    str(output_path),
                    "--project-id",
                    project_id,
                    "--candidate-id",
                    candidate_id,
                ),
                cwd=self.root,
                staging_directory=staging,
                expected_outputs=("candidate.json",),
                env=normal_environment,
                secret_env=secret_environment,
                timeout_seconds=900,
            )
            try:
                result = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkflowConflict(
                    "Reference-format analysis returned an unreadable result."
                ) from exc
        if not isinstance(result, dict):
            raise WorkflowConflict(
                "Reference-format analysis returned an invalid result."
            )
        if not self._reference_candidate_is_isolated(result):
            raise WorkflowConflict(
                "Reference analysis failed the content-isolation gate; the uploaded review was not added."
            )
        return result

    def register_reference(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        filename: str,
        content_base64: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        self._matrix(principal, project_id)
        safe_name = Path(str(filename or "")).name
        suffix = Path(safe_name).suffix.casefold()
        if suffix not in {".pdf", ".docx", ".md", ".txt"}:
            raise WorkflowValidationError(
                "Upload a PDF, DOCX, Markdown, or text review document."
            )
        try:
            raw = base64.b64decode(str(content_base64 or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkflowValidationError("Reference content is not valid base64.") from exc
        if not raw:
            raise WorkflowValidationError("Uploaded reference file is empty.")
        if len(raw) > 30 * 1024 * 1024:
            raise WorkflowValidationError("Uploaded reference file exceeds 30 MB.")
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        matrix_ids = _paper_ids(matrix["rows"])
        candidate_id = f"reference-{uuid.uuid4().hex[:12]}"
        analysis = self._analyze_reference_document(
            principal,
            project_id,
            candidate_id=candidate_id,
            safe_name=safe_name,
            raw=raw,
            matrix=matrix,
        )
        outline_text = self._validate_outline(
            str(analysis.get("outline_md") or ""), set(matrix_ids)
        )
        analysis_mode = str(analysis.get("analysis_mode") or "")
        references, _references_artifact = self._read_json(
            principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
        )
        index = deepcopy(references or {"project_id": project_id, "candidates": []})
        source_logical = f"planning/references/{candidate_id}/{safe_name}"
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={
                    source_logical: (raw, suffix.lstrip(".")),
                },
                input_snapshot={"filename": safe_name},
            )
            candidate = {
                "candidate_id": candidate_id,
                "outline_style": f"reference:{candidate_id}",
                "labels": {"en": safe_name, "zh": f"参考大纲：{safe_name}"},
                "outline_md": outline_text,
                "source": "reference",
                "source_name": safe_name,
                "source_artifact_id": published[source_logical].id,
                "analysis_mode": analysis_mode,
                "content_source": "current_matrix_only",
                "reference_content_reused": False,
                "content_firewall": deepcopy(analysis.get("content_firewall") or {}),
                "reference_structure_metrics": deepcopy(
                    analysis.get("reference_structure_metrics") or {}
                ),
                "writing_style": deepcopy(analysis.get("writing_style") or {}),
                "created_at": utc_now().isoformat(),
            }
            index["candidates"] = [*(index.get("candidates") or []), candidate]
            index_published, index_run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={REFERENCE_INDEX_LOGICAL_NAME: (_json_bytes(index), "json")},
                input_snapshot={"source_artifact_id": published[source_logical].id},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={
                    REFERENCE_INDEX_LOGICAL_NAME: index_published[
                        REFERENCE_INDEX_LOGICAL_NAME
                    ].id
                },
                run_id=index_run.id,
                expected_revision=revision,
                status="review",
            )
        return {
            "project_id": project_id,
            "candidate": candidate,
            "matrix_revision": state.revision,
        }

    def generate_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if not outline.get("outline_complete") or not str(outline.get("outline_md") or "").strip():
            raise WorkflowConflict(
                "The selected outline is blank or incomplete. Edit and save it before Blueprint generation."
            )
        matrix_ids = set(_paper_ids(matrix["rows"]))
        parsed = _outline_sections(str(outline["outline_md"]))
        matrix_order = _paper_ids(matrix["rows"])
        prepared = []
        for index, section in enumerate(parsed, start=1):
            role = infer_section_role(
                section.get("title"), section.get("section_role")
            )
            if role == "references":
                continue
            assigned = list(dict.fromkeys(section["paper_ids"]))
            unknown = sorted(set(assigned) - matrix_ids)
            if unknown:
                raise WorkflowConflict(
                    "The selected outline refers to papers missing from the current Matrix.",
                    details={"paper_ids": unknown},
                )
            prepared.append(
                {
                    **section,
                    "section_id": f"S{len(prepared) + 1:02d}",
                    "section_role": role,
                    "paper_ids": assigned,
                }
            )

        normalized, primary_owner = assign_primary_paper_sections(
            prepared, matrix_order
        )
        sections: list[dict[str, Any]] = []
        for section in normalized:
            role = section["section_role"]
            primary = list(section["primary_papers"])
            supporting = list(section["supporting_papers"])
            evidence_papers = list(dict.fromkeys([*primary, *supporting]))
            if role == "introduction":
                thesis = (
                    "Define the review scope, organizing question, and evidence landscape "
                    "without repeating paper-level results from the body sections."
                )
                problem = "What problem, scope, and organizing logic does this review establish?"
                claim = (
                    "Frame the field and its evidence boundaries with brief representative "
                    "citations; reserve detailed study descriptions for their primary sections."
                )
                figure_need = "None unless an overview figure materially clarifies the review scope."
                target_words = 900
            elif role == "conclusion":
                thesis = (
                    "Synthesize cross-section findings, limitations, and future directions "
                    "without replaying individual paper summaries."
                )
                problem = "What conclusions hold across sections, and where do important limits remain?"
                claim = (
                    "Compare the body-section conclusions and cite prior evidence concisely; "
                    "do not restate full methods, conditions, or paper-by-paper results."
                )
                figure_need = "None unless a cross-section synthesis figure adds new comparative value."
                target_words = 900
            else:
                thesis = f"Synthesize evidence for {section['title']}."
                problem = f"What does the current evidence establish about {section['title']}?"
                if primary:
                    claim = (
                        f"Develop claim-centered synthesis from {len(primary)} primary papers, "
                        "comparing convergent evidence, differences, and limitations."
                    )
                else:
                    claim = (
                        "Develop a cross-cutting comparison from previously introduced evidence "
                        "without repeating full study descriptions."
                    )
                figure_need = f"Support the comparison in {section['title']} where source evidence permits."
                target_words = max(700, 350 * max(1, len(primary)))
            sections.append(
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "section_role": role,
                    "section_thesis": thesis,
                    "review_problem": problem,
                    "major_papers": primary,
                    "primary_papers": primary,
                    "supporting_papers": supporting,
                    "review_claims": [{"claim": claim}],
                    "figure_or_table_needs": [
                        {
                            "type": "Figure or table",
                            "purpose": figure_need,
                            "candidate_papers": primary[:3],
                        }
                    ],
                    "avoid_patterns": [
                        "Do not infer unsupported conditions or mechanisms.",
                        "Do not repeat a paper-level description already owned by another section.",
                        "Do not organize prose as one title or one summary block per paper.",
                    ],
                    "section_transition": "Connect this evidence to the next comparison axis.",
                    "target_words": target_words,
                }
            )
        if not sections:
            raise WorkflowValidationError("The selected outline contains no usable sections.")
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if matrix_state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        blueprint = {
            "project_id": project_id,
            "review_topic": str(
                matrix.get("review_topic") or (discovery or {}).get("topic") or ""
            ),
            "outline_style": outline.get("outline_style"),
            "source_matrix_artifact_id": matrix_artifact.id,
            "source_outline_artifact_id": outline_artifact.id,
            "rule_pack": "general",
            "rule_pack_path": "references/rule_packs/general",
            "generated_at": utc_now().isoformat(),
            "paper_assignment_policy": {
                "mode": "single_primary_section_with_supporting_cross_references",
                "primary_section_by_paper": primary_owner,
                "introduction_and_conclusion_are_synthesis_only": True,
            },
            "sections": sections,
            "section_writing_plan_md": "# Section Writing Plan\n\n"
            + "\n".join(
                f"- {section['section_id']} {section['title']}: "
                f"{len(section['primary_papers'])} primary, "
                f"{len(section['supporting_papers'])} supporting papers."
                for section in sections
            )
            + "\n",
        }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="blueprint",
                files={BLUEPRINT_LOGICAL_NAME: (_json_bytes(blueprint), "json")},
                input_snapshot={
                    "matrix_artifact_id": matrix_artifact.id,
                    "outline_artifact_id": outline_artifact.id,
                },
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "blueprint",
                artifact_ids={BLUEPRINT_LOGICAL_NAME: published[BLUEPRINT_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
                approve_stages={"matrix": matrix_state.revision},
            )
        return {
            "project_id": project_id,
            "section_blueprint": blueprint,
            "blueprint_artifact_id": published[BLUEPRINT_LOGICAL_NAME].id,
            "blueprint_revision": state.revision,
            "matrix_revision": matrix_state.revision + 1,
        }

    def confirm_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        blueprint, _blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        _matrix, matrix_artifact = self._matrix(principal, project_id)
        _outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if (
            blueprint.get("source_matrix_artifact_id") != matrix_artifact.id
            or blueprint.get("source_outline_artifact_id") != outline_artifact.id
        ):
            raise WorkflowConflict(
                "Blueprint is out of date. Regenerate it from the current Matrix and outline."
            )
        state = self.repository.compare_and_set_stage(
            principal.user_id,
            project_id,
            "blueprint",
            int(revision),
            status="approved",
        )
        return {
            "project_id": project_id,
            "revision": state.revision,
            "status": state.status,
            "next_stage": "sections",
            "next_path": f"/sections?project={project_id}",
        }
