"""PostgreSQL-native Matrix, outline, and Blueprint workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import uuid
import zipfile
from html import unescape
from copy import deepcopy
from pathlib import Path
from typing import Any

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository


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
    },
    "catalyst": {
        "en": "Catalyst and method-classified",
        "zh": "按催化剂与方法分类",
        "axis": "catalysts, methods, and operating principles",
    },
    "reaction": {
        "en": "Reaction-type-classified",
        "zh": "按反应类型分类",
        "axis": "transformation and mechanistic strategy",
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
            current = {"title": heading.group(1).strip(), "paper_ids": []}
            sections.append(current)
            continue
        if current is not None and line.casefold().startswith("assigned papers:"):
            current["paper_ids"] = list(dict.fromkeys(re.findall(r"\b[A-Za-z]+\d+\b", line)))
    return sections


class PlanningService:
    def __init__(self, repository: WorkflowRepository, artifacts: ArtifactService):
        self.repository = repository
        self.artifacts = artifacts
        self._write_lock = threading.RLock()

    def _owned_project(self, principal: Principal, project_id: str):
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

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
    def _outline_document(style: str, rows: list[dict[str, Any]]) -> str:
        definition = OUTLINE_STYLES[style]
        ids = _paper_ids(rows)
        introduction = ids[: min(6, len(ids))]
        midpoint = max(1, (len(ids) + 1) // 2)
        first, second = ids[:midpoint], ids[midpoint:]
        lines = [
            "# Selected Outline",
            "",
            f"Primary structure: {definition['en']}.",
            "This working outline remains fully editable before Blueprint generation.",
            "",
            "## 1. Introduction",
            f"Assigned papers: {', '.join(introduction)}.",
            "Purpose: define scope, terminology, and the evidence-comparison criteria.",
            "",
            f"## 2. Evidence organized by {definition['axis']}",
            f"Assigned papers: {', '.join(first)}.",
            f"Purpose: compare the literature through {definition['axis']}.",
            "",
        ]
        if second:
            lines.extend(
                [
                    "## 3. Complementary systems and limitations",
                    f"Assigned papers: {', '.join(second)}.",
                    "Purpose: contrast complementary systems, evidence boundaries, and limitations.",
                    "",
                ]
            )
        lines.extend(
            [
                "## 4. Cross-category comparison and conclusion",
                f"Assigned papers: {', '.join(ids)}.",
                "Purpose: synthesize trends, unresolved questions, and future directions.",
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
        missing = [section["title"] for section in sections if not section["paper_ids"]]
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
        selection_fingerprint = hashlib.sha256(
            "\n".join(sorted(selected_ids)).encode("utf-8")
        ).hexdigest()
        matrix_sync = dict(matrix.get("sync") or {})
        selection_current = bool(
            selected_ids
            and matrix_sync.get("selection_fingerprint") == selection_fingerprint
            and set(_paper_ids(rows)) == set(selected_ids)
        )
        generated = [
            {
                "candidate_id": style,
                "outline_style": style,
                "labels": {"en": definition["en"], "zh": definition["zh"]},
                "outline_md": self._outline_document(style, rows),
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
        reference_candidates = list((references or {}).get("candidates") or [])
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
                "selection_fingerprint": selection_fingerprint,
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
            markdown = self._outline_document(style, rows)
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
        if suffix in {".md", ".txt"}:
            outline_text = raw.decode("utf-8", errors="replace")
        elif suffix == ".docx":
            try:
                from io import BytesIO

                with zipfile.ZipFile(BytesIO(raw)) as archive:
                    xml = archive.read("word/document.xml").decode(
                        "utf-8", errors="ignore"
                    )
            except (OSError, KeyError, zipfile.BadZipFile) as exc:
                raise WorkflowValidationError("The DOCX reference is unreadable.") from exc
            outline_text = unescape(
                re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
            )
        else:
            try:
                from io import BytesIO
                from pypdf import PdfReader

                outline_text = "\n".join(
                    page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages
                )
            except Exception as exc:
                raise WorkflowValidationError("The PDF reference is unreadable.") from exc
        matrix_ids = _paper_ids(matrix["rows"])
        try:
            outline_text = self._validate_outline(outline_text, set(matrix_ids))
            analysis_mode = "provided_outline"
        except WorkflowValidationError:
            headings: list[str] = []
            for raw_line in outline_text.splitlines():
                value = re.sub(r"\s+", " ", raw_line).strip()
                match = re.match(
                    r"^(?:#{1,6}\s+|\d+(?:\.\d+){0,3}[.)]?\s+|[IVXLC]+[.)]\s+)(.+?)$",
                    value,
                    flags=re.IGNORECASE,
                )
                if match:
                    title = match.group(1).strip(" -.:;")
                    if title and title not in headings:
                        headings.append(title)
            headings = headings[: max(1, min(20, len(matrix_ids)))]
            if not headings:
                headings = ["Reference review organization"]
            groups = [[] for _heading in headings]
            for index, paper_id in enumerate(matrix_ids):
                groups[index % len(groups)].append(paper_id)
            lines = ["# Reference-derived Outline", ""]
            for index, (heading, assigned) in enumerate(zip(headings, groups), start=1):
                lines.extend(
                    [
                        f"## {index}. {heading}",
                        f"Assigned papers: {', '.join(assigned)}.",
                        "Purpose: adapt this reference-review section to the current Matrix evidence.",
                        "",
                    ]
                )
            outline_text = self._validate_outline("\n".join(lines), set(matrix_ids))
            analysis_mode = "heading_extraction"
        candidate_id = f"reference-{uuid.uuid4().hex[:12]}"
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
        sections: list[dict[str, Any]] = []
        for index, section in enumerate(parsed, start=1):
            assigned = list(dict.fromkeys(section["paper_ids"]))
            unknown = sorted(set(assigned) - matrix_ids)
            if unknown:
                raise WorkflowConflict(
                    "The selected outline refers to papers missing from the current Matrix.",
                    details={"paper_ids": unknown},
                )
            sections.append(
                {
                    "section_id": f"S{index:02d}",
                    "title": section["title"],
                    "section_thesis": f"Synthesize evidence for {section['title']}.",
                    "review_problem": f"What does the current evidence establish about {section['title']}?",
                    "major_papers": assigned,
                    "review_claims": [
                        {
                            "claim": f"Compare the evidence, limitations, and implications across {len(assigned)} assigned papers."
                        }
                    ],
                    "figure_or_table_needs": [
                        {
                            "type": "Figure or table",
                            "purpose": f"Support the comparison in {section['title']} where source evidence permits.",
                            "candidate_papers": assigned[:3],
                        }
                    ],
                    "avoid_patterns": ["Do not infer unsupported conditions or mechanisms."],
                    "section_transition": "Connect this evidence to the next comparison axis.",
                    "target_words": max(700, 350 * len(assigned)),
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
            "sections": sections,
            "section_writing_plan_md": "# Section Writing Plan\n\n"
            + "\n".join(
                f"- {section['section_id']} {section['title']}: {len(section['major_papers'])} assigned papers."
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
