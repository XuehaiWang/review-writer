"""PostgreSQL-native draft assembly, editing, quality, rewrite, and approval."""

from __future__ import annotations

import json
import hashlib
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import database_session, utc_now
from review_writer_api.domain_services.planning import (
    BLUEPRINT_LOGICAL_NAME,
    MATRIX_LOGICAL_NAME,
)
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.paragraph_markers import ensure_prose_paragraph_markers
from review_writer_core.publication_caption import normalize_publication_caption
from review_writer_core.publication_voice import publication_voice_issues


DRAFT_DOCUMENT = "draft/manuscript.md"
DRAFT_QUALITY = "draft/quality.json"
DRAFT_REWRITES = "draft/rewrite-candidates.json"
DRAFT_OPTIMIZATIONS = "draft/optimization-proposals.json"
DRAFT_OVERLAYS = "draft/rewrite-overlays.json"
DRAFT_APPROVAL = "draft/approval.json"
SECTION_INDEX = "sections/section_drafts.json"
FIGURE_MANIFEST = "figures/manifest.json"
PARAGRAPH_MARKER = re.compile(
    r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->"
)


class DraftNotReady(WorkflowConflict):
    code = "DRAFT_NOT_READY"


class DraftApprovalBlocked(WorkflowConflict):
    code = "DRAFT_APPROVAL_BLOCKED"


class DraftsService:
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

    def _artifact(self, principal: Principal, project_id: str, logical_name: str):
        self._owned_project(principal, project_id)
        return self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )

    def _read_text(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[str, ArtifactRecord | None]:
        artifact = self._artifact(principal, project_id, logical_name)
        if artifact is None:
            if required:
                raise WorkflowNotFound("Current workflow artifact not found.")
            return "", None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            return resolved.path.read_text(encoding="utf-8"), artifact
        except OSError as exc:
            raise WorkflowConflict("The current workflow artifact is unreadable.") from exc

    def _read_json(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[dict[str, Any], ArtifactRecord | None]:
        text, artifact = self._read_text(
            principal, project_id, logical_name, required=required
        )
        if artifact is None:
            return {}, None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowConflict("The current workflow artifact is invalid.") from exc
        if not isinstance(value, dict):
            raise WorkflowConflict("The current workflow artifact is invalid.")
        return value, artifact

    def _publish_files(
        self,
        principal: Principal,
        project_id: str,
        files: dict[
            str,
            tuple[bytes | Callable[[dict[str, ArtifactRecord]], bytes], str],
        ],
        *,
        expected_revision: int,
        status: str = "review",
        metadata: dict[str, Any] | None = None,
        approval_events: list[dict[str, Any]] | None = None,
        expected_current_artifacts: dict[str, str] | None = None,
        expected_stage_states: dict[str, dict[str, Any]] | None = None,
        invalidate_final: bool = True,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            "draft",
            status="succeeded",
            input_snapshot=dict(metadata or {}),
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        published: dict[str, ArtifactRecord] = {}
        for index, (logical_name, (content_or_builder, artifact_type)) in enumerate(files.items()):
            content = (
                content_or_builder(published)
                if callable(content_or_builder)
                else content_or_builder
            )
            suffix = Path(logical_name).suffix or ".bin"
            filename = f"{index:03d}-{uuid.uuid4().hex}{suffix}"
            (staging / filename).write_bytes(content)
            published[logical_name] = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                filename,
                logical_name=logical_name,
                artifact_type=artifact_type,
                producer_stage="draft",
                make_current=False,
                metadata=dict(metadata or {}),
            )
        state = self.repository.promote_stage_artifacts_atomically(
            principal.user_id,
            project_id,
            "draft",
            artifact_ids={name: record.id for name, record in published.items()},
            run_id=run.id,
            expected_revision=int(expected_revision),
            status=status,
            invalidate_stages=("final",) if invalidate_final else (),
            approval_events=approval_events,
            expected_current_artifacts=expected_current_artifacts,
            expected_stage_states=expected_stage_states,
        )
        return published, state

    @staticmethod
    def _paragraph_spans(markdown: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for marker in PARAGRAPH_MARKER.finditer(markdown or ""):
            prefix = markdown[: marker.start()].rstrip()
            start = prefix.rfind("\n\n") + 2
            text = prefix[start:].strip()
            if not text or text.startswith(("#", "![", "<!--")):
                continue
            rows.append(
                {
                    "paragraph_id": marker.group(1),
                    "text": text,
                    "start": start,
                    "end": len(prefix),
                    "marker_end": marker.end(),
                }
            )
        return rows

    @staticmethod
    def _normalized(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    @classmethod
    def _text_sha256(cls, text: str) -> str:
        return hashlib.sha256(cls._normalized(text).encode("utf-8")).hexdigest()

    @classmethod
    def _apply_rewrite_overlays(
        cls, markdown: str, overlays: dict[str, Any]
    ) -> tuple[str, dict[str, list[str]]]:
        entries = overlays.get("entries") if isinstance(overlays, dict) else {}
        if not isinstance(entries, dict) or not entries:
            return markdown, {"applied": [], "conflicts": []}
        updated = markdown
        applied: list[str] = []
        conflicts: list[str] = []
        for raw_id, raw_entry in entries.items():
            paragraph_id = str(raw_id)
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            paragraph = next(
                (
                    row
                    for row in cls._paragraph_spans(updated)
                    if row["paragraph_id"] == paragraph_id
                ),
                None,
            )
            rewritten = str(entry.get("rewritten_text") or "").strip()
            if (
                paragraph is None
                or not rewritten
                or cls._text_sha256(str(paragraph["text"]))
                != str(entry.get("source_text_sha256") or "")
            ):
                conflicts.append(paragraph_id)
                continue
            updated = (
                updated[: paragraph["start"]]
                + rewritten
                + updated[paragraph["end"] :]
            )
            applied.append(paragraph_id)
        return updated, {"applied": applied, "conflicts": conflicts}

    @classmethod
    def _optimization_candidate(
        cls, current_text: str, model_text: str
    ) -> tuple[str, list[dict[str, str]]]:
        """Build a reviewable candidate from paragraph bodies only."""
        source_paragraphs = cls._paragraph_spans(current_text)
        model_paragraphs = {
            str(row["paragraph_id"]): row for row in cls._paragraph_spans(model_text)
        }
        changes: list[dict[str, str]] = []
        replacements: list[tuple[int, int, str]] = []
        for source in source_paragraphs:
            paragraph_id = str(source["paragraph_id"])
            candidate = model_paragraphs.get(paragraph_id)
            if candidate is None:
                continue
            original_text = str(source["text"])
            candidate_text = str(candidate["text"]).strip()
            if cls._normalized(candidate_text) == cls._normalized(original_text):
                continue
            changes.append(
                {
                    "paragraph_id": paragraph_id,
                    "original_text": original_text,
                    "candidate_text": candidate_text,
                }
            )
            replacements.append(
                (int(source["start"]), int(source["end"]), candidate_text)
            )
        candidate_draft = current_text
        for start, end, replacement in reversed(replacements):
            candidate_draft = (
                candidate_draft[:start] + replacement + candidate_draft[end:]
            )
        return candidate_draft.rstrip() + "\n", changes

    @classmethod
    def _optimization_candidate_from_changes(
        cls, current_text: str, changes: list[dict[str, Any]]
    ) -> str:
        requested = {
            str(item.get("paragraph_id") or ""): str(
                item.get("candidate_text") or ""
            ).strip()
            for item in changes
            if str(item.get("paragraph_id") or "").strip()
            and str(item.get("candidate_text") or "").strip()
        }
        replacements: list[tuple[int, int, str]] = []
        found: set[str] = set()
        for paragraph in cls._paragraph_spans(current_text):
            paragraph_id = str(paragraph["paragraph_id"])
            replacement = requested.get(paragraph_id)
            if replacement is None:
                continue
            replacements.append(
                (int(paragraph["start"]), int(paragraph["end"]), replacement)
            )
            found.add(paragraph_id)
        missing = sorted(set(requested) - found)
        if missing:
            raise WorkflowConflict(
                "Optimization paragraph markers are no longer current: "
                + ", ".join(missing)
            )
        candidate = current_text
        for start, end, replacement in reversed(replacements):
            candidate = candidate[:start] + replacement + candidate[end:]
        return candidate.rstrip() + "\n"

    @staticmethod
    def _assemble_markdown(
        title: str,
        section_index: dict[str, Any],
        figure_manifest: dict[str, Any],
        matrix: dict[str, Any],
    ) -> str:
        figures_by_paragraph: dict[str, list[dict[str, Any]]] = {}
        for row in figure_manifest.get("figures") or []:
            if not isinstance(row, dict) or row.get("status") != "redrawn":
                continue
            paragraph_id = str(row.get("target_paragraph_id") or "")
            output_id = str(row.get("output_artifact_id") or "")
            if paragraph_id and output_id:
                figures_by_paragraph.setdefault(paragraph_id, []).append(row)
        parts = [f"# {title}"]
        figure_number = 0
        matrix_rows = [
            row for row in matrix.get("rows") or [] if isinstance(row, dict)
        ]
        matrix_by_id = {
            str(row.get("paper_id") or ""): row
            for row in matrix_rows
            if str(row.get("paper_id") or "").strip()
        }
        citation_numbers = {
            paper_id: index
            for index, paper_id in enumerate(matrix_by_id, start=1)
        }
        cited_paper_ids: set[str] = set()
        for section in section_index.get("sections") or []:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or section.get("section_id") or "Section")
            parts.append(f"## {heading}")
            paragraphs = [
                row for row in section.get("paragraphs") or [] if isinstance(row, dict)
            ]
            if not paragraphs:
                draft = str(section.get("draft_md") or "").strip()
                if draft:
                    parts.append(draft)
                continue
            for paragraph in paragraphs:
                paragraph_id = str(paragraph.get("paragraph_id") or "")
                text = str(paragraph.get("text") or "").strip()
                if not paragraph_id or not text:
                    continue
                cited_papers = [
                    str(value).strip()
                    for value in (
                        paragraph.get("cited_paper_ids")
                        or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                    )
                    if str(value).strip()
                ]
                callouts = []
                for paper_id in cited_papers:
                    if paper_id not in citation_numbers:
                        citation_numbers[paper_id] = len(citation_numbers) + 1
                    callouts.append(citation_numbers[paper_id])
                    cited_paper_ids.add(paper_id)
                if callouts:
                    callout = f"[{', '.join(str(value) for value in callouts)}]"
                    if re.search(r"\s*\[(?:\d+[\d,;\s-]*)\]\s*$", text):
                        text = re.sub(
                            r"\s*\[(?:\d+[\d,;\s-]*)\]\s*$",
                            f" {callout}",
                            text,
                        )
                    else:
                        text = f"{text} {callout}"
                figure_blocks: list[str] = []
                figure_callouts: list[str] = []
                for figure in figures_by_paragraph.get(paragraph_id, []):
                    figure_number += 1
                    output_id = str(figure["output_artifact_id"])
                    paper_id = str(figure.get("paper_id") or "").strip()
                    caption_text = str(figure.get("source_caption_text") or "").strip()
                    normalized_caption = normalize_publication_caption(caption_text)
                    caption_body = str(
                        normalized_caption.publication_text
                        or figure.get("publication_caption_text")
                        or ""
                    ).strip()
                    caption_plain = str(
                        normalized_caption.plain_text
                        or figure.get("publication_caption_plain_text")
                        or figure.get("source_label")
                        or f"Figure {figure_number}"
                    ).strip()
                    interpretation_basis = (
                        "source_caption"
                        if caption_text
                        and not re.fullmatch(
                            r"(?:figure|scheme|table)\s*\d+[a-z]?[.:]?",
                            caption_text,
                            re.IGNORECASE,
                        )
                        else "identity_only"
                    )
                    role = str(figure.get("representative_role") or "paper_overview")
                    role_text = {
                        "core_transformation": "core transformation",
                        "mechanism": "proposed mechanistic framework",
                        "scope": "reported scope or result pattern",
                        "paper_overview": "overall research strategy",
                    }.get(role, "overall research strategy")
                    figure_callouts.append(
                        f"Figure {figure_number} provides visual context for the paper's {role_text}."
                    )
                    metadata = json.dumps(
                        {
                            "figure_id": figure.get("figure_id"),
                            "paper_id": paper_id,
                            "target_paragraph_id": paragraph_id,
                            "output_artifact_id": output_id,
                            "representative_role": role,
                            "published_label": f"Figure {figure_number}",
                            "interpretation_basis": interpretation_basis,
                            "caption_normalization_status": normalized_caption.status,
                            "caption_normalization_version": normalized_caption.version,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    figure_blocks.append(
                        "\n".join(
                            (
                                f"<!-- inserted_figure: {metadata} -->",
                                f"![{caption_plain}](/api/v1/artifacts/{output_id}/content)",
                                (
                                    f"*Figure {figure_number}. {caption_body}*"
                                    if caption_body
                                    else f"*Figure {figure_number}.*"
                                ),
                            )
                        )
                    )
                if figure_callouts:
                    text = f"{text} {' '.join(figure_callouts)}"
                parts.append(f"{text}\n\n<!-- paragraph_id: {paragraph_id} -->")
                parts.extend(figure_blocks)
        if cited_paper_ids:
            references = ["## References"]
            for paper_id in sorted(cited_paper_ids, key=citation_numbers.__getitem__):
                number = citation_numbers[paper_id]
                row = matrix_by_id.get(paper_id, {})

                def value(field: str) -> Any:
                    raw = row.get(field)
                    return raw.get("value") if isinstance(raw, dict) else raw

                raw_authors = value("authors")
                authors = ", ".join(map(str, raw_authors)) if isinstance(raw_authors, list) else str(raw_authors or "").strip()
                reference_parts = [
                    authors,
                    str(value("title") or "").strip(),
                    str(value("journal") or "").strip(),
                    str(value("year") or "").strip(),
                    str(value("doi") or "").strip(),
                ]
                reference = ". ".join(part.rstrip(".") for part in reference_parts if part)
                if not reference:
                    reference = f"Paper P{number:03d}"
                references.append(f"[{number}] {reference}")
            parts.append("\n".join(references))
        return "\n\n".join(part.strip() for part in parts if part.strip()) + "\n"

    def assemble(self, principal: Principal, project_id: str) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        figures_state = self.repository.get_stage_state(
            principal.user_id, project_id, "figures"
        )
        if figures_state is None or figures_state.status != "approved":
            raise DraftNotReady("Approve the current figure stage before assembling Draft.")
        sections, sections_artifact = self._read_json(
            principal, project_id, SECTION_INDEX
        )
        matrix, matrix_artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        manifest, manifest_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST
        )
        overlays, overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        expected_revision = state.revision if state else 0
        project = self._owned_project(principal, project_id)
        markdown = self._assemble_markdown(
            str(project.topic or project.slug or project_id),
            sections,
            manifest,
            matrix,
        )
        markdown, overlay_replay = self._apply_rewrite_overlays(markdown, overlays)
        with self._write_lock:
            published, next_state = self._publish_files(
                principal,
                project_id,
                {DRAFT_DOCUMENT: (markdown.encode("utf-8"), "markdown")},
                expected_revision=expected_revision,
                metadata={
                    "source_sections_artifact_id": sections_artifact.id,
                    "source_figure_manifest_artifact_id": manifest_artifact.id,
                    "source_matrix_artifact_id": matrix_artifact.id,
                    "source_rewrite_overlay_artifact_id": (
                        overlay_artifact.id if overlay_artifact else ""
                    ),
                    "overlay_replay": overlay_replay,
                    "operation": "assemble",
                },
                expected_current_artifacts={
                    SECTION_INDEX: sections_artifact.id,
                    FIGURE_MANIFEST: manifest_artifact.id,
                    MATRIX_LOGICAL_NAME: matrix_artifact.id,
                },
                expected_stage_states={
                    "figures": {
                        "revision": figures_state.revision,
                        "status": "approved",
                    }
                },
            )
        return {
            "project_id": project_id,
            "draft_artifact_id": published[DRAFT_DOCUMENT].id,
            "revision": next_state.revision,
            "overlay_replay": overlay_replay,
        }

    def _freshness(
        self, principal: Principal, project_id: str, draft: ArtifactRecord | None
    ) -> dict[str, Any]:
        sections = self._artifact(principal, project_id, SECTION_INDEX)
        figures = self._artifact(principal, project_id, FIGURE_MANIFEST)
        matrix = self._artifact(principal, project_id, MATRIX_LOGICAL_NAME)
        metadata = dict(draft.metadata if draft else {})
        upstream_stale = bool(
            draft
            and (
                not sections
                or not figures
                or not matrix
                or metadata.get("source_sections_artifact_id") != sections.id
                or metadata.get("source_figure_manifest_artifact_id") != figures.id
                or metadata.get("source_matrix_artifact_id") != matrix.id
            )
        )
        return {
            "source_stale": upstream_stale,
            "draft_stale": False,
            "figures_stale": upstream_stale,
            "upstream_stale": upstream_stale,
            "editing_blocked": upstream_stale,
            "stale": upstream_stale,
        }

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        text, draft_artifact = self._read_text(
            principal, project_id, DRAFT_DOCUMENT, required=False
        )
        quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        rewrites, _rewrite_artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES, required=False
        )
        optimizations, _optimization_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS, required=False
        )
        overlays, _overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        approval, _approval_artifact = self._read_json(
            principal, project_id, DRAFT_APPROVAL, required=False
        )
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        paragraphs = self._paragraph_spans(text)
        paragraph_by_id = {row["paragraph_id"]: row for row in paragraphs}
        manifest, _manifest_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        matrix, _matrix_artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME, required=False
        )
        matrix_paper_ids = [
            str(row.get("paper_id") or "")
            for row in matrix.get("rows") or []
            if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
        ]
        display_width = max(3, len(str(len(matrix_paper_ids))))
        paper_display_labels = {
            paper_id: f"P{index:0{display_width}d}"
            for index, paper_id in enumerate(matrix_paper_ids, start=1)
        }
        images_by_paragraph: dict[str, list[dict[str, str]]] = {}
        for row in manifest.get("figures") or []:
            if not isinstance(row, dict):
                continue
            paragraph_id = str(row.get("target_paragraph_id") or "")
            output_id = str(row.get("output_artifact_id") or "")
            if paragraph_id and output_id:
                figure_id = str(row.get("figure_id") or "")
                paper_id = str(row.get("paper_id") or "")
                paper_label = paper_display_labels.get(paper_id, paper_id)
                display_figure_id = (
                    f"{paper_label}{figure_id[len(paper_id):]}"
                    if paper_id and figure_id.startswith(paper_id)
                    else figure_id
                )
                images_by_paragraph.setdefault(paragraph_id, []).append(
                    {
                        "figure_id": display_figure_id,
                        "source_figure_id": figure_id,
                        "artifact_id": output_id,
                        "url": f"/api/v1/artifacts/{output_id}/content",
                    }
                )
        quality_current = bool(
            draft_artifact
            and quality_artifact
            and quality.get("source_draft_artifact_id") == draft_artifact.id
        )
        public_quality = dict(quality) if quality else {}
        issues = []
        for issue in public_quality.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            paragraph_id = str(issue.get("paragraph_id") or "")
            paragraph = paragraph_by_id.get(paragraph_id) or {
                "paragraph_id": paragraph_id,
                "text": "",
            }
            issues.append(
                {
                    **issue,
                    "paragraph": {
                        "paragraph_id": paragraph_id,
                        "text": paragraph.get("text", ""),
                        "images": images_by_paragraph.get(paragraph_id, []),
                    },
                }
            )
        if public_quality:
            public_quality["issues"] = issues
            public_quality["current"] = quality_current
            if not quality_current:
                public_quality["status"] = "stale"
        approval_current = bool(
            draft_artifact
            and state
            and state.status == "approved"
            and approval.get("status") == "approved"
            and approval.get("draft_artifact_id") == draft_artifact.id
            and quality_artifact
            and approval.get("quality_artifact_id") == quality_artifact.id
            and quality_current
        )
        versions = self.repository.list_artifacts(
            principal.user_id, project_id, DRAFT_DOCUMENT
        )
        rewrite_candidates = []
        for value in (rewrites.get("entries") or {}).values():
            if not isinstance(value, dict):
                continue
            candidate = dict(value)
            if (
                candidate.get("status") == "pending"
                and (
                    not draft_artifact
                    or candidate.get("source_draft_artifact_id") != draft_artifact.id
                    or not quality_artifact
                    or candidate.get("source_quality_artifact_id")
                    != quality_artifact.id
                )
            ):
                candidate["status"] = "stale"
            rewrite_candidates.append(candidate)
        optimization_proposals = []
        for value in (optimizations.get("entries") or {}).values():
            if not isinstance(value, dict):
                continue
            proposal = dict(value)
            if (
                proposal.get("status") == "pending"
                and (
                    not draft_artifact
                    or proposal.get("source_draft_artifact_id") != draft_artifact.id
                )
            ):
                proposal["status"] = "stale"
            # The full candidate manuscript and candidate quality snapshot are
            # server-side review data.  The UI only needs paragraph-level diffs.
            proposal.pop("candidate_draft_text", None)
            proposal.pop("candidate_quality", None)
            proposal.pop("rewrite_overlays", None)
            proposal.pop("source_quality", None)
            proposal.pop("source_overlays", None)
            proposal["changes"] = [
                {
                    key: item
                    for key, item in dict(change).items()
                    if key != "candidate_evaluation"
                }
                for change in proposal.get("changes") or []
                if isinstance(change, dict)
            ]
            optimization_proposals.append(proposal)
        jobs = self.repository.list_project_jobs(principal.user_id, project_id)
        feedback_jobs = [
            job
            for job in jobs
            if job.job_type in {
                "draft.evaluate",
                "draft.optimize",
                "draft.rewrite",
                "draft.accept-rewrite",
            }
        ]
        latest_feedback_job = feedback_jobs[0] if feedback_jobs else None
        active_feedback_job = next(
            (
                job for job in feedback_jobs
                if job.status in {"queued", "running", "cancel_requested"}
            ),
            None,
        )
        rewrite_states: dict[str, dict[str, Any]] = {}
        for job in jobs:
            if job.job_type != "draft.rewrite":
                continue
            paragraph_id = str(job.payload.get("paragraph_id") or "")
            rewrite_states.setdefault(
                paragraph_id,
                {
                    "status": "completed" if job.status == "succeeded" else job.status,
                    "job_id": job.id,
                    "error": job.error_message,
                },
            )
        voice_issues = publication_voice_issues(text)
        return {
            "project_id": project_id,
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "draft_artifact_id": draft_artifact.id if draft_artifact else "",
            "first_draft_md": text,
            "publication_voice": {
                "status": "warning" if voice_issues else "pass",
                "issues": voice_issues,
            },
            "paragraphs": [
                {key: value for key, value in row.items() if key not in {"start", "end", "marker_end"}}
                for row in paragraphs
            ],
            "quality": public_quality,
            "quality_artifact_id": quality_artifact.id if quality_artifact else "",
            "rewrite_candidates": rewrite_candidates,
            "optimization_proposals": optimization_proposals,
            "rewrite_states": rewrite_states,
            "active_feedback_job_id": (
                active_feedback_job.id if active_feedback_job else ""
            ),
            "active_feedback_job_type": (
                active_feedback_job.job_type if active_feedback_job else ""
            ),
            "latest_feedback_job_id": (
                latest_feedback_job.id if latest_feedback_job else ""
            ),
            "latest_feedback_job_type": (
                latest_feedback_job.job_type if latest_feedback_job else ""
            ),
            "latest_feedback_job_status": (
                latest_feedback_job.status if latest_feedback_job else ""
            ),
            "rewrite_overlay_count": len(overlays.get("entries") or {}),
            "overlay_replay": dict(
                (draft_artifact.metadata if draft_artifact else {}).get(
                    "overlay_replay"
                )
                or {}
            ),
            "draft_approval": approval,
            "draft_approval_current": approval_current,
            "versions": [
                {
                    "artifact_id": version.id,
                    "current": bool(draft_artifact and version.id == draft_artifact.id),
                    "operation": str(version.metadata.get("operation") or "saved"),
                    "created_at": (
                        version.created_at.isoformat() if version.created_at else ""
                    ),
                }
                for version in versions
            ],
            "freshness": self._freshness(principal, project_id, draft_artifact),
        }

    def save_text(
        self,
        principal: Principal,
        project_id: str,
        *,
        text: str,
        revision: int,
        operation: str = "full-edit",
        approval_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        if self._freshness(principal, project_id, current)["upstream_stale"]:
            raise DraftNotReady("Draft inputs changed. Reassemble Draft before editing.")
        canonical = str(text).rstrip() + "\n"
        if canonical == current_text:
            raise WorkflowValidationError("The edited draft has no content change.")
        metadata = dict(current.metadata)
        metadata["operation"] = operation
        metadata["previous_draft_artifact_id"] = current.id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {DRAFT_DOCUMENT: (canonical.encode("utf-8"), "markdown")},
                expected_revision=revision,
                metadata=metadata,
                approval_events=approval_events,
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
            )
        return {
            "draft_artifact_id": published[DRAFT_DOCUMENT].id,
            "revision": state.revision,
        }

    def save_paragraph(
        self,
        principal: Principal,
        project_id: str,
        paragraph_id: str,
        *,
        text: str,
        revision: int,
    ) -> dict[str, Any]:
        markdown, _current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        paragraph = next(
            (row for row in self._paragraph_spans(markdown) if row["paragraph_id"] == paragraph_id),
            None,
        )
        if paragraph is None:
            raise WorkflowNotFound("Draft paragraph not found.")
        updated = markdown[: paragraph["start"]] + str(text).strip() + markdown[paragraph["end"] :]
        return self.save_text(
            principal,
            project_id,
            text=updated,
            revision=revision,
            operation=f"paragraph-edit:{paragraph_id}",
        )

    def restore(
        self,
        principal: Principal,
        project_id: str,
        *,
        artifact_id: str,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        resolved = self.artifacts.resolve_owned_artifact(
            principal.user_id, artifact_id
        )
        artifact = resolved.artifact
        if artifact.project_id != project_id or artifact.logical_name != DRAFT_DOCUMENT:
            raise WorkflowNotFound("Draft version not found.")
        current = self._artifact(principal, project_id, DRAFT_DOCUMENT)
        if current is None:
            raise WorkflowNotFound("Current Draft version not found.")
        if current.id == artifact.id:
            raise WorkflowValidationError("The selected Draft version is already current.")
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "draft-version",
            "subject_id": artifact_id,
            "decision": "undo",
            "details": {
                "restored_artifact_id": artifact_id,
                "replaced_artifact_id": current.id if current else "",
            },
            "created_at": utc_now(),
        }
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            "draft",
            status="succeeded",
            input_snapshot={
                "operation": "restore",
                "restored_artifact_id": artifact.id,
            },
        )
        with self._write_lock:
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "draft",
                artifact_ids={DRAFT_DOCUMENT: artifact.id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=("final",),
                approval_events=[event],
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
            )
        return {"draft_artifact_id": artifact.id, "revision": state.revision}

    def evaluation_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        goal: float,
        paragraph_goal: float = 85.0,
        max_iterations: int = 3,
        min_case_words: int = 140,
        max_case_words: int = 280,
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        if not payload["draft_artifact_id"]:
            raise DraftNotReady("Assemble and save Draft before evaluation.")
        if payload["freshness"]["upstream_stale"]:
            raise DraftNotReady("Draft inputs changed. Reassemble Draft before evaluation.")
        marked_text, marker_report = ensure_prose_paragraph_markers(
            payload["first_draft_md"]
        )
        if int(marker_report.get("prose_paragraph_count") or 0) < 1:
            raise DraftNotReady("The current Draft contains no prose paragraphs to evaluate.")
        if marker_report.get("changed"):
            self.save_text(
                principal,
                project_id,
                text=marked_text,
                revision=int(payload["revision"]),
                operation="evaluation-marker-normalization",
            )
            payload = self.get(principal, project_id)
        safe_min_words = max(1, int(min_case_words))
        safe_max_words = max(1, int(max_case_words))
        if safe_max_words < safe_min_words:
            raise WorkflowValidationError(
                "The maximum case word count must not be lower than the minimum."
            )
        return {
            **self.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "expected_revision": payload["revision"],
            "draft_text": payload["first_draft_md"],
            "paragraphs": payload["paragraphs"],
            "goal": max(0.0, min(float(goal), 100.0)),
            "paragraph_goal": max(0.0, min(float(paragraph_goal), 100.0)),
            "max_iterations": max(1, min(int(max_iterations), 10)),
            "min_case_words": safe_min_words,
            "max_case_words": safe_max_words,
        }

    def compatibility_payload(
        self, principal: Principal, project_id: str
    ) -> dict[str, Any]:
        matrix, _matrix_artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME, required=False
        )
        sections, _sections_artifact = self._read_json(
            principal, project_id, SECTION_INDEX, required=False
        )
        figures, _figures_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        blueprint, _blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        overlays, _overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        artifact_paths: dict[str, str] = {}
        for row in figures.get("figures") or []:
            if not isinstance(row, dict):
                continue
            artifact_id = str(row.get("output_artifact_id") or "")
            if artifact_id:
                artifact_paths[artifact_id] = str(
                    self.artifacts.resolve_owned_artifact(
                        principal.user_id, artifact_id
                    ).path
                )
        paper_ids = {
            str(row.get("paper_id") or "")
            for row in matrix.get("rows") or []
            if isinstance(row, dict) and row.get("paper_id")
        }
        library_metadata: dict[str, dict[str, Any]] = {}
        if paper_ids:
            with database_session(self.repository.session_factory) as session:
                rows = session.scalars(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == uuid.UUID(principal.user_id),
                        LibraryPaper.paper_id.in_(tuple(paper_ids)),
                        LibraryPaper.deleted_at.is_(None),
                    )
                ).all()
                library_metadata = {
                    row.paper_id: dict(row.metadata_json or {}) for row in rows
                }
        return {
            "matrix": matrix,
            "blueprint": blueprint,
            "section_index": sections,
            "figure_manifest": figures,
            "figure_artifact_paths": artifact_paths,
            "library_metadata": library_metadata,
            "rewrite_overlays": overlays,
        }

    def publish_evaluation(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        current = self._artifact(principal, project_id, DRAFT_DOCUMENT)
        if current is None or current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while evaluation was running.")
        score = max(0.0, min(float(built.get("score") or 0), 100.0))
        quality = {
            **{key: value for key, value in built.items() if key != "source_draft_artifact_id"},
            "source_draft_artifact_id": current.id,
            "score": score,
            "goal": float(built.get("goal") or job_payload.get("goal") or 90),
            "status": "completed",
            "evaluated_at": utc_now().isoformat(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_QUALITY: (
                        (json.dumps(quality, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={"source_draft_artifact_id": current.id, "operation": "evaluate"},
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
                invalidate_final=False,
            )
        return {
            "quality_artifact_id": published[DRAFT_QUALITY].id,
            "score": score,
            "feedback_status": (
                dict(built.get("feedback_status"))
                if isinstance(built.get("feedback_status"), dict)
                else {}
            ),
            "revision": state.revision,
        }

    def publish_optimization(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        if current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while batch optimization was running.")
        review_changes = [
            dict(item)
            for item in built.get("review_changes") or []
            if isinstance(item, dict)
        ]
        review_candidate_text = str(
            built.get("review_candidate_draft_text") or ""
        )
        model_text = str(
            review_candidate_text
            if review_changes and review_candidate_text.strip()
            else built.get("draft_text")
            or ""
        ).rstrip() + "\n"
        if not model_text.strip():
            raise WorkflowValidationError("Batch optimization returned no Draft content.")
        score = max(
            0.0,
            min(
                float(
                    (
                        built.get("review_candidate_score")
                        if review_changes
                        else built.get("score")
                    )
                    or 0
                ),
                100.0,
            ),
        )
        quality_base = {
            key: value
            for key, value in built.items()
            if key
            not in {
                "draft_text",
                "rewrite_overlays",
                "source_draft_artifact_id",
                "review_candidate_draft_text",
                "review_candidate_score",
                "review_changes",
                "review_excluded",
            }
        }
        quality_base.update(
            {
                "score": score,
                "goal": float(built.get("goal") or job_payload.get("goal") or 90),
                "status": "completed",
                "evaluated_at": utc_now().isoformat(),
            }
        )
        feedback_status = (
            dict(built.get("feedback_status"))
            if isinstance(built.get("feedback_status"), dict)
            else {}
        )

        # Only paragraph bodies may enter a batch proposal.  Rebuilding from
        # the current manuscript prevents a model from silently changing
        # headings, figure markers, references, or document structure outside
        # the reviewable paragraph comparisons.
        candidate_text, changes = self._optimization_candidate(
            current_text, model_text
        )
        review_change_by_id = {
            str(item.get("paragraph_id") or ""): item
            for item in review_changes
            if str(item.get("paragraph_id") or "")
        }
        changes = [
            {
                **change,
                **{
                    key: value
                    for key, value in review_change_by_id.get(
                        str(change.get("paragraph_id") or ""), {}
                    ).items()
                    if key
                    not in {"paragraph_id", "original_text", "candidate_text"}
                },
            }
            for change in changes
        ]
        draft_changed = bool(changes) and candidate_text != current_text

        if not draft_changed:
            # There is nothing for the user to approve.  Keep the current
            # manuscript and its evaluation intact and report the no-op.
            return {
                "draft_artifact_id": current.id,
                "quality_artifact_id": "",
                "score": score,
                "draft_changed": False,
                "proposal_created": False,
                "rewrite_accepted": int(feedback_status.get("rewrite_accepted") or 0),
                "rewrite_rejected": int(feedback_status.get("rewrite_rejected") or 0),
                "rewrite_deferred": int(feedback_status.get("rewrite_deferred") or 0),
                "feedback_status": feedback_status,
                "revision": int(job_payload["expected_revision"]),
            }

        current_quality, current_quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        source_evaluated_score = max(
            0.0, min(float(built.get("score") or 0), 100.0)
        )
        source_quality = {
            **quality_base,
            "score": source_evaluated_score,
            "total_score": source_evaluated_score,
            "source_draft_artifact_id": current.id,
        }
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS, required=False
        )
        entries = dict(store.get("entries") or {})
        proposal_id = str(uuid.uuid4())
        created_at = utc_now().isoformat()
        entries[proposal_id] = {
            "proposal_id": proposal_id,
            "source_draft_artifact_id": current.id,
            "source_quality_artifact_id": (
                current_quality_artifact.id if current_quality_artifact else ""
            ),
            "candidate_draft_text": candidate_text,
            "candidate_quality": quality_base,
            "source_quality": source_quality,
            "rewrite_overlays": (
                built.get("rewrite_overlays")
                if isinstance(built.get("rewrite_overlays"), dict)
                else (
                    job_payload.get("rewrite_overlays")
                    if isinstance(job_payload.get("rewrite_overlays"), dict)
                    else {}
                )
            ),
            "source_overlays": (
                dict(job_payload.get("rewrite_overlays") or {})
                if isinstance(job_payload.get("rewrite_overlays"), dict)
                else {}
            ),
            "changes": changes,
            "source_score": float(source_quality.get("score") or 0),
            "candidate_score": score,
            "feedback_status": feedback_status,
            "status": "pending",
            "created_at": created_at,
        }
        proposal_payload = {"project_id": project_id, "entries": entries}
        expected_currents = {DRAFT_DOCUMENT: current.id}
        if store_artifact is not None:
            expected_currents[DRAFT_OPTIMIZATIONS] = store_artifact.id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_OPTIMIZATIONS: (
                        (
                            json.dumps(
                                proposal_payload, ensure_ascii=False, indent=2
                            )
                            + "\n"
                        ).encode(),
                        "json",
                    )
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "batch-optimization-proposal",
                    "proposal_id": proposal_id,
                    "source_draft_artifact_id": current.id,
                    "feedback_status": feedback_status,
                },
                expected_current_artifacts=expected_currents,
                invalidate_final=False,
            )
        return {
            "draft_artifact_id": current.id,
            "quality_artifact_id": "",
            "score": score,
            "draft_changed": False,
            "proposal_created": True,
            "proposal_id": proposal_id,
            "proposal_artifact_id": published[DRAFT_OPTIMIZATIONS].id,
            "change_count": len(changes),
            "rewrite_accepted": int(feedback_status.get("rewrite_accepted") or 0),
            "rewrite_rejected": int(feedback_status.get("rewrite_rejected") or 0),
            "rewrite_deferred": int(feedback_status.get("rewrite_deferred") or 0),
            "feedback_status": feedback_status,
            "revision": state.revision,
        }

    def decide_optimization_proposal(
        self,
        principal: Principal,
        project_id: str,
        proposal_id: str,
        *,
        decision: str,
        revision: int,
        selected_paragraph_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise WorkflowValidationError("Unknown optimization proposal decision.")
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS
        )
        entries = dict(store.get("entries") or {})
        proposal = dict(entries.get(proposal_id) or {})
        if not proposal:
            raise WorkflowNotFound("Optimization proposal not found.")
        if proposal.get("status") != "pending":
            raise WorkflowConflict("Optimization proposal was already decided.")
        current_text, current = self._read_text(
            principal, project_id, DRAFT_DOCUMENT
        )
        if proposal.get("source_draft_artifact_id") != current.id:
            raise WorkflowConflict("Optimization proposal is stale for the current Draft.")

        proposal_changes = [
            dict(item)
            for item in proposal.get("changes") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "").strip()
        ]
        available_ids = {
            str(item.get("paragraph_id") or "") for item in proposal_changes
        }
        requested_ids = [
            str(value).strip()
            for value in selected_paragraph_ids or []
            if str(value).strip()
        ]
        if decision == "accept":
            selected_ids = set(requested_ids or sorted(available_ids))
            unknown = sorted(selected_ids - available_ids)
            if unknown:
                raise WorkflowValidationError(
                    "Unknown optimization paragraph selection: "
                    + ", ".join(unknown)
                )
            if not selected_ids:
                raise WorkflowValidationError(
                    "Select at least one optimized paragraph to save."
                )
        else:
            selected_ids = set()
        selected_changes = [
            item
            for item in proposal_changes
            if str(item.get("paragraph_id") or "") in selected_ids
        ]

        decided_at = utc_now().isoformat()
        proposal["status"] = "accepted" if decision == "accept" else "rejected"
        proposal["decided_at"] = decided_at
        proposal["selected_paragraph_ids"] = sorted(selected_ids)
        proposal["discarded_paragraph_ids"] = sorted(available_ids - selected_ids)
        entries[proposal_id] = proposal
        if decision == "accept":
            for other_id, other_value in list(entries.items()):
                if other_id == proposal_id or not isinstance(other_value, dict):
                    continue
                other = dict(other_value)
                if (
                    other.get("status") == "pending"
                    and other.get("source_draft_artifact_id") == current.id
                ):
                    other["status"] = "superseded"
                    other["superseded_by_proposal_id"] = proposal_id
                    other["decided_at"] = decided_at
                    entries[other_id] = other

        files: dict[
            str,
            tuple[bytes | Callable[[dict[str, ArtifactRecord]], bytes], str],
        ] = {
            DRAFT_OPTIMIZATIONS: (
                (
                    json.dumps(
                        {"project_id": project_id, "entries": entries},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode(),
                "json",
            )
        }
        if decision == "accept":
            candidate_text = self._optimization_candidate_from_changes(
                current_text, selected_changes
            )
            if not candidate_text.strip() or candidate_text == current_text:
                raise WorkflowConflict("Optimization proposal contains no applicable change.")
            files[DRAFT_DOCUMENT] = (
                (candidate_text.rstrip() + "\n").encode("utf-8"),
                "markdown",
            )

            source_quality = dict(
                proposal.get("source_quality")
                or proposal.get("candidate_quality")
                or {}
            )
            candidate_quality = source_quality
            scored_changes = 0
            for change in selected_changes:
                paragraph_id = str(change.get("paragraph_id") or "")
                candidate_evaluation = dict(
                    change.get("candidate_evaluation") or {}
                )
                if (
                    candidate_evaluation.get("evaluation_scope")
                    == "single_paragraph"
                    and str(candidate_evaluation.get("paragraph_id") or "")
                    == paragraph_id
                ):
                    candidate_quality = self._incremental_quality(
                        candidate_quality,
                        candidate_evaluation,
                        paragraph_id=paragraph_id,
                        source_quality_artifact_id=str(
                            proposal.get("source_quality_artifact_id") or ""
                        ),
                    )
                    scored_changes += 1
            if scored_changes != len(selected_changes):
                if len(selected_changes) != len(proposal_changes):
                    raise WorkflowConflict(
                        "This legacy batch proposal cannot publish a partial selection "
                        "because it has no paragraph-level candidate scores."
                    )
                candidate_quality = dict(
                    proposal.get("candidate_quality") or candidate_quality
                )
            candidate_quality.update(
                {
                    "quality_scope": "batch_selected_paragraphs",
                    "selected_paragraph_ids": sorted(selected_ids),
                    "status": "completed",
                    "evaluated_at": decided_at,
                }
            )

            def quality_content(published: dict[str, ArtifactRecord]) -> bytes:
                quality = {
                    **candidate_quality,
                    "source_draft_artifact_id": published[DRAFT_DOCUMENT].id,
                    "status": "completed",
                    "evaluated_at": decided_at,
                }
                return (
                    json.dumps(quality, ensure_ascii=False, indent=2) + "\n"
                ).encode()

            files[DRAFT_QUALITY] = (quality_content, "json")
            rewrite_overlays = dict(proposal.get("source_overlays") or {})
            overlay_entries = dict(rewrite_overlays.get("entries") or {})
            for change in selected_changes:
                paragraph_id = str(change.get("paragraph_id") or "")
                overlay_entries[paragraph_id] = {
                    "paragraph_id": paragraph_id,
                    "source_text_sha256": self._text_sha256(
                        str(change.get("original_text") or "")
                    ),
                    "rewritten_text": str(change.get("candidate_text") or ""),
                    "updated_at": decided_at,
                }
            rewrite_overlays.update(
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "entries": overlay_entries,
                }
            )
            files[DRAFT_OVERLAYS] = (
                (
                    json.dumps(
                        rewrite_overlays, ensure_ascii=False, indent=2
                    )
                    + "\n"
                ).encode(),
                "json",
            )

        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "batch-optimization-proposal",
            "subject_id": proposal_id,
            "decision": decision,
            "details": {
                "change_count": len(selected_changes),
                "selected_paragraph_ids": sorted(selected_ids),
                "source_draft_artifact_id": current.id,
                "candidate_score": proposal.get("candidate_score"),
            },
            "created_at": utc_now(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=revision,
                metadata={
                    **dict(current.metadata),
                    "operation": f"batch-optimization-{decision}",
                    "proposal_id": proposal_id,
                    "previous_draft_artifact_id": current.id,
                },
                approval_events=[event],
                expected_current_artifacts={
                    DRAFT_DOCUMENT: current.id,
                    DRAFT_OPTIMIZATIONS: store_artifact.id,
                },
                invalidate_final=decision == "accept",
            )
        return {
            "proposal_id": proposal_id,
            "decision": decision,
            "selected_paragraph_ids": sorted(selected_ids),
            "draft_artifact_id": (
                published[DRAFT_DOCUMENT].id
                if DRAFT_DOCUMENT in published
                else current.id
            ),
            "quality_artifact_id": (
                published[DRAFT_QUALITY].id
                if DRAFT_QUALITY in published
                else ""
            ),
            "revision": state.revision,
        }

    def rewrite_payload(
        self, principal: Principal, project_id: str, paragraph_id: str
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        quality = payload.get("quality") or {}
        if not payload.get("quality_artifact_id") or not quality:
            raise DraftNotReady(
                "Evaluate the Draft once before requesting a paragraph rewrite."
            )
        paragraph = next(
            (row for row in payload["paragraphs"] if row["paragraph_id"] == paragraph_id),
            None,
        )
        if paragraph is None:
            raise WorkflowNotFound("Draft paragraph not found.")
        matching_issues = [
            issue
            for issue in quality.get("issues") or []
            if issue.get("paragraph_id") == paragraph_id
        ]
        if not matching_issues:
            raise DraftNotReady("This paragraph is not in the current issue queue.")
        preflight = quality.get("preflight")
        preflight = preflight if isinstance(preflight, dict) else {}
        paragraph_check = next(
            (
                item
                for item in preflight.get("paragraph_checks") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        case_range = preflight.get("case_word_range")
        if isinstance(case_range, (list, tuple)) and len(case_range) >= 2:
            min_case_words = int(case_range[0] or 140)
            max_case_words = int(case_range[1] or 280)
        elif isinstance(case_range, dict):
            min_case_words = int(case_range.get("min_words") or 140)
            max_case_words = int(case_range.get("max_words") or 280)
        else:
            min_case_words, max_case_words = 140, 280
        return {
            **self.compatibility_payload(principal, project_id),
            "project_id": project_id,
            # The native rewrite handler materializes ``first_draft.md`` from
            # this field before invoking the feedback-loop CLI.  Keeping only
            # ``paragraph_text`` is insufficient because the CLI validates the
            # selected paragraph against the complete, evaluated draft.
            "draft_text": payload["first_draft_md"],
            "paragraph_id": paragraph_id,
            "paragraph_text": paragraph["text"],
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "expected_revision": payload["revision"],
            "quality": quality,
            "issues": matching_issues,
            "goal": float(quality.get("goal") or quality.get("pass_threshold") or 90),
            "paragraph_goal": float(
                quality.get("paragraph_pass_threshold")
                or quality.get("paragraph_goal")
                or 85
            ),
            "min_case_words": min_case_words,
            "max_case_words": max_case_words,
            "word_range_applicable": bool(
                paragraph_check.get("word_range_applicable", True)
            ),
        }

    def publish_rewrite_candidate(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        current = self._artifact(principal, project_id, DRAFT_DOCUMENT)
        current_quality = self._artifact(principal, project_id, DRAFT_QUALITY)
        if current is None or current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while rewrite was running.")
        if (
            current_quality is None
            or current_quality.id != job_payload["source_quality_artifact_id"]
        ):
            raise WorkflowConflict("Draft evaluation changed while rewrite was running.")
        original = str(job_payload["paragraph_text"])
        paragraph_id = str(job_payload["paragraph_id"])
        source_paragraph_evaluation = dict(
            built.get("source_paragraph_evaluation") or {}
        )
        candidate_evaluation = dict(built.get("candidate_evaluation") or {})
        candidate_score_entry = dict(candidate_evaluation.get("paragraph_score") or {})
        if (
            candidate_evaluation.get("evaluation_scope") != "single_paragraph"
            or str(candidate_evaluation.get("paragraph_id") or "") != paragraph_id
            or str(candidate_score_entry.get("paragraph_id") or "") != paragraph_id
        ):
            raise WorkflowValidationError(
                "The generated candidate did not receive a valid paragraph-only score."
            )
        source_score_entry = dict(
            source_paragraph_evaluation.get("paragraph_score") or {}
        )
        if str(source_score_entry.get("paragraph_id") or "") != paragraph_id:
            raise WorkflowValidationError(
                "The candidate comparison is missing the source paragraph score."
            )
        try:
            source_paragraph_score = float(source_score_entry["score"])
            candidate_paragraph_score = float(candidate_score_entry["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowValidationError(
                "The candidate comparison contains an invalid paragraph score."
            ) from exc
        candidate_text = str(built.get("candidate_text") or "").strip()
        if not candidate_text:
            raise WorkflowValidationError("AI rewrite returned no candidate text.")
        if self._normalized(candidate_text) == self._normalized(original):
            raise WorkflowValidationError(
                "AI rewrite made no normalized content change; it was not accepted as a candidate."
            )
        store, _artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES, required=False
        )
        entries = dict(store.get("entries") or {})
        candidate_id = str(uuid.uuid4())
        entries[candidate_id] = {
            "candidate_id": candidate_id,
            "paragraph_id": job_payload["paragraph_id"],
            "source_draft_artifact_id": current.id,
            "source_quality_artifact_id": current_quality.id,
            "original_text": original,
            "candidate_text": candidate_text,
            "candidate_text_sha256": self._text_sha256(candidate_text),
            "resolved_issue_ids": list(built.get("resolved_issue_ids") or []),
            "generation_report": dict(built.get("report") or {}),
            "route": str(dict(built.get("report") or {}).get("route") or ""),
            "rewrite_mode": str(
                dict(built.get("report") or {}).get("rewrite_mode") or ""
            ),
            "requires_manual_confirmation": bool(
                dict(built.get("report") or {}).get(
                    "requires_manual_confirmation", False
                )
            ),
            "source_paragraph_evaluation": source_paragraph_evaluation,
            "candidate_evaluation": candidate_evaluation,
            "source_paragraph_score": round(source_paragraph_score, 2),
            "candidate_paragraph_score": round(candidate_paragraph_score, 2),
            "status": "pending",
            "created_at": utc_now().isoformat(),
        }
        payload = {"project_id": project_id, "entries": entries}
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_REWRITES: (
                        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={"operation": "rewrite-candidate", "paragraph_id": job_payload["paragraph_id"]},
                expected_current_artifacts={
                    DRAFT_DOCUMENT: current.id,
                    DRAFT_QUALITY: current_quality.id,
                },
                invalidate_final=False,
            )
        return {
            "candidate_id": candidate_id,
            "candidate_artifact_id": published[DRAFT_REWRITES].id,
            "paragraph_id": paragraph_id,
            "source_paragraph_score": round(source_paragraph_score, 2),
            "candidate_paragraph_score": round(candidate_paragraph_score, 2),
            "revision": state.revision,
        }

    def accept_rewrite_payload(
        self,
        principal: Principal,
        project_id: str,
        candidate_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Build an immutable payload for publishing one scored candidate."""

        payload = self.get(principal, project_id)
        quality = dict(payload.get("quality") or {})
        if not payload.get("quality_artifact_id") or not quality:
            raise DraftNotReady("The rewrite candidate has no evaluation context.")
        if int(payload.get("revision") or 0) != int(revision):
            raise WorkflowConflict("Draft revision changed. Refresh and try again.")
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES
        )
        candidate = dict((store.get("entries") or {}).get(candidate_id) or {})
        if not candidate:
            raise WorkflowNotFound("Rewrite candidate not found.")
        if candidate.get("status") != "pending":
            raise WorkflowConflict("Rewrite candidate was already decided.")
        if candidate.get("source_draft_artifact_id") != payload["draft_artifact_id"]:
            raise WorkflowConflict("Rewrite candidate is stale for the current Draft.")
        if candidate.get("source_quality_artifact_id") != payload["quality_artifact_id"]:
            raise WorkflowConflict("Rewrite candidate is stale for the current evaluation.")
        paragraph_id = str(candidate.get("paragraph_id") or "")
        paragraph = next(
            (
                row
                for row in self._paragraph_spans(payload["first_draft_md"])
                if row["paragraph_id"] == paragraph_id
            ),
            None,
        )
        if paragraph is None or self._normalized(paragraph["text"]) != self._normalized(
            candidate.get("original_text")
        ):
            raise WorkflowConflict("Rewrite paragraph changed after candidate generation.")
        candidate_text = str(candidate.get("candidate_text") or "").strip()
        if not candidate_text:
            raise WorkflowConflict("Rewrite candidate contains no text.")
        candidate_text_sha256 = str(candidate.get("candidate_text_sha256") or "")
        if candidate_text_sha256 and candidate_text_sha256 != self._text_sha256(
            candidate_text
        ):
            raise WorkflowConflict("Rewrite candidate integrity check failed.")
        candidate_evaluation = dict(candidate.get("candidate_evaluation") or {})
        if candidate_evaluation and (
            candidate_evaluation.get("evaluation_scope") != "single_paragraph"
            or str(candidate_evaluation.get("paragraph_id") or "") != paragraph_id
            or str(
                dict(candidate_evaluation.get("paragraph_score") or {}).get(
                    "paragraph_id"
                )
                or ""
            )
            != paragraph_id
        ):
            raise WorkflowConflict("Stored candidate score is invalid.")
        candidate_draft = (
            payload["first_draft_md"][: paragraph["start"]]
            + candidate_text
            + payload["first_draft_md"][paragraph["end"] :]
        ).rstrip() + "\n"
        matching_issues = [
            issue
            for issue in quality.get("issues") or []
            if isinstance(issue, dict) and issue.get("paragraph_id") == paragraph_id
        ]
        allowed_unsupported_claims = sorted(
            {
                str(value)
                for issue in matching_issues
                for value in issue.get("unsupported_claims") or []
                if str(value).strip()
            }
        )
        preflight = quality.get("preflight")
        preflight = preflight if isinstance(preflight, dict) else {}
        paragraph_check = next(
            (
                item
                for item in preflight.get("paragraph_checks") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        case_range = preflight.get("case_word_range")
        if isinstance(case_range, (list, tuple)) and len(case_range) >= 2:
            min_case_words = int(case_range[0] or 140)
            max_case_words = int(case_range[1] or 280)
        elif isinstance(case_range, dict):
            min_case_words = int(case_range.get("min_words") or 140)
            max_case_words = int(case_range.get("max_words") or 280)
        else:
            min_case_words, max_case_words = 140, 280
        return {
            **self.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "candidate_id": candidate_id,
            "paragraph_id": paragraph_id,
            "paragraph_text": str(paragraph["text"]),
            "candidate_text": candidate_text,
            # New candidates are scored before human review.  The router uses
            # this immutable evaluation directly; the legacy evaluator is only
            # a compatibility fallback for candidates created by older builds.
            "candidate_evaluation": candidate_evaluation,
            "candidate_draft_text": candidate_draft,
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "source_rewrites_artifact_id": store_artifact.id,
            "expected_revision": int(revision),
            "quality": quality,
            "goal": float(quality.get("goal") or quality.get("pass_threshold") or 90),
            "paragraph_goal": float(
                quality.get("paragraph_pass_threshold")
                or quality.get("paragraph_goal")
                or 85
            ),
            "min_case_words": min_case_words,
            "max_case_words": max_case_words,
            "word_range_applicable": bool(
                paragraph_check.get("word_range_applicable", True)
            ),
            "allowed_unsupported_claims": allowed_unsupported_claims,
        }

    @staticmethod
    def _replace_scoped_rows(
        rows: Any,
        paragraph_id: str,
        replacements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        kept = [
            dict(item)
            for item in rows or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") != paragraph_id
        ]
        return kept + [dict(item) for item in replacements if isinstance(item, dict)]

    def _incremental_quality(
        self,
        current_quality: dict[str, Any],
        built: dict[str, Any],
        *,
        paragraph_id: str,
        source_quality_artifact_id: str,
    ) -> dict[str, Any]:
        paragraph_score = dict(built.get("paragraph_score") or {})
        if str(paragraph_score.get("paragraph_id") or "") != paragraph_id:
            raise WorkflowValidationError(
                "Targeted evaluation returned the wrong paragraph identity."
            )
        scores = [
            dict(item)
            for item in current_quality.get("paragraph_scores") or []
            if isinstance(item, dict)
        ]
        old = next(
            (item for item in scores if str(item.get("paragraph_id") or "") == paragraph_id),
            None,
        )
        if old is None:
            raise WorkflowConflict(
                "The current full evaluation has no score for this paragraph."
            )
        old_paragraph_score = max(0.0, min(float(old.get("score") or 0), 100.0))
        new_paragraph_score = max(
            0.0, min(float(paragraph_score.get("score") or 0), 100.0)
        )
        paragraph_score["score"] = round(new_paragraph_score, 2)
        paragraph_scores = self._replace_scoped_rows(
            scores, paragraph_id, [paragraph_score]
        )
        paragraph_count = max(1, len(paragraph_scores))
        previous_score = max(
            0.0, min(float(current_quality.get("score") or 0), 100.0)
        )
        score_delta = (new_paragraph_score - old_paragraph_score) / paragraph_count
        updated_score = round(max(0.0, min(previous_score + score_delta, 100.0)), 2)
        paragraph_goal = float(
            current_quality.get("paragraph_pass_threshold")
            or current_quality.get("paragraph_goal")
            or 85
        )
        paragraph_failures = [
            item
            for item in paragraph_scores
            if str(item.get("route") or "") != "pass"
            or str(item.get("severity") or "") in {"critical", "major"}
        ]
        blocking = [
            item
            for item in paragraph_scores
            if float(item.get("score") or 0) < paragraph_goal
            or str(item.get("severity") or "") in {"critical", "major"}
            or str(item.get("route") or "") not in {"pass", "final_polish"}
        ]
        issues = self._replace_scoped_rows(
            current_quality.get("issues"), paragraph_id, []
        )
        if paragraph_score in paragraph_failures:
            issues.append(
                {
                    **paragraph_score,
                    "issue_id": f"incremental-{paragraph_id}-{uuid.uuid4().hex[:8]}",
                    "message": str(
                        paragraph_score.get("diagnosis")
                        or "Review this paragraph."
                    ),
                }
            )
        source_check = dict(current_quality.get("source_check") or {})
        source_entry = built.get("source_check_entry")
        if isinstance(source_entry, dict) and source_entry:
            source_check["entries"] = self._replace_scoped_rows(
                source_check.get("entries"), paragraph_id, [source_entry]
            )
            counts: dict[str, int] = {}
            for item in source_check.get("entries") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("source_check_status") or "not_assessed")
                counts[key] = counts.get(key, 0) + 1
            source_check["counts"] = dict(sorted(counts.items()))
        preflight = dict(current_quality.get("preflight") or {})
        local_preflight = built.get("local_preflight")
        if isinstance(local_preflight, dict):
            for key in ("paragraph_checks", "paragraph_findings"):
                preflight[key] = self._replace_scoped_rows(
                    preflight.get(key),
                    paragraph_id,
                    [
                        item
                        for item in local_preflight.get(key) or []
                        if isinstance(item, dict)
                    ],
                )
        hard_gate_failures = {
            str(value)
            for value in current_quality.get("hard_gate_failures") or []
            if str(value).strip()
            and str(value) != "paragraph_readability_or_source_failures"
        }
        hard_gate_failures.update(
            str(value)
            for value in built.get("local_hard_gate_failures") or []
            if str(value).strip()
            and str(value) != "paragraph_readability_or_source_failures"
        )
        if any(
            str(item.get("severity") or "") in {"critical", "major"}
            for item in preflight.get("paragraph_findings") or []
            if isinstance(item, dict)
        ):
            hard_gate_failures.add("paragraph_readability_or_source_failures")
        hard_gate_failures = sorted(hard_gate_failures)
        goal = float(current_quality.get("goal") or current_quality.get("pass_threshold") or 90)
        decision = (
            "PASS"
            if updated_score >= goal and not hard_gate_failures and not blocking
            else "REGENERATE_SECTIONS"
        )
        history = [
            dict(item)
            for item in current_quality.get("incremental_evaluations") or []
            if isinstance(item, dict)
        ][-99:]
        history.append(
            {
                "paragraph_id": paragraph_id,
                "old_paragraph_score": round(old_paragraph_score, 2),
                "new_paragraph_score": round(new_paragraph_score, 2),
                "previous_overall_score": round(previous_score, 2),
                "updated_overall_score": updated_score,
                "paragraph_count": paragraph_count,
                "overall_score_delta": round(score_delta, 4),
                "source_quality_artifact_id": source_quality_artifact_id,
                "evaluated_at": str(built.get("evaluated_at") or utc_now().isoformat()),
            }
        )
        return {
            **current_quality,
            "score": updated_score,
            "total_score": updated_score,
            "decision": decision,
            "status": "completed",
            "paragraph_scores": paragraph_scores,
            "paragraph_failures": paragraph_failures,
            "blocking_paragraph_failures": blocking,
            "issues": issues,
            "hard_gate_failures": hard_gate_failures,
            "source_check": source_check,
            "preflight": preflight,
            "quality_scope": "incremental_paragraph",
            "last_evaluated_paragraph_id": paragraph_id,
            "last_incremental_dimension_scores": list(
                built.get("local_dimension_scores") or []
            ),
            "incremental_evaluations": history,
            "evaluated_at": str(built.get("evaluated_at") or utc_now().isoformat()),
        }

    def publish_accepted_rewrite(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish one accepted paragraph using its precomputed candidate score."""

        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        current_quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY
        )
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES
        )
        if current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while paragraph evaluation was running.")
        if quality_artifact.id != job_payload["source_quality_artifact_id"]:
            raise WorkflowConflict(
                "Draft evaluation changed while paragraph evaluation was running."
            )
        if store_artifact.id != job_payload["source_rewrites_artifact_id"]:
            raise WorkflowConflict(
                "Rewrite candidates changed while paragraph evaluation was running."
            )
        candidate_id = str(job_payload["candidate_id"])
        entries = dict(store.get("entries") or {})
        candidate = dict(entries.get(candidate_id) or {})
        if not candidate or candidate.get("status") != "pending":
            raise WorkflowConflict("Rewrite candidate was already decided.")
        if str(candidate.get("candidate_text") or "").strip() != str(
            job_payload.get("candidate_text") or ""
        ).strip():
            raise WorkflowConflict("Rewrite candidate changed during evaluation.")
        stored_evaluation = dict(candidate.get("candidate_evaluation") or {})
        if stored_evaluation:
            stored_score = dict(stored_evaluation.get("paragraph_score") or {})
            built_score = dict(built.get("paragraph_score") or {})
            if (
                str(stored_evaluation.get("paragraph_id") or "")
                != str(job_payload["paragraph_id"])
                or stored_score.get("score") != built_score.get("score")
            ):
                raise WorkflowConflict(
                    "The precomputed candidate score changed before saving."
                )
        paragraph_id = str(job_payload["paragraph_id"])
        candidate_draft = str(job_payload.get("candidate_draft_text") or "")
        if not candidate_draft.strip() or candidate_draft == current_text:
            raise WorkflowConflict("Rewrite candidate contains no applicable change.")

        updated_quality = self._incremental_quality(
            current_quality,
            built,
            paragraph_id=paragraph_id,
            source_quality_artifact_id=quality_artifact.id,
        )
        decided_at = utc_now().isoformat()
        candidate.update(
            {
                "status": "accepted",
                "decided_at": decided_at,
                "paragraph_score_before": next(
                    (
                        item.get("score")
                        for item in current_quality.get("paragraph_scores") or []
                        if isinstance(item, dict)
                        and str(item.get("paragraph_id") or "") == paragraph_id
                    ),
                    None,
                ),
                "paragraph_score_after": dict(built.get("paragraph_score") or {}).get(
                    "score"
                ),
                "overall_score_after": updated_quality["score"],
            }
        )
        entries[candidate_id] = candidate
        for other_id, other_value in list(entries.items()):
            if other_id == candidate_id or not isinstance(other_value, dict):
                continue
            other = dict(other_value)
            if (
                other.get("status") == "pending"
                and other.get("source_draft_artifact_id") == current.id
            ):
                other["status"] = "superseded"
                other["superseded_by_candidate_id"] = candidate_id
                other["decided_at"] = decided_at
                entries[other_id] = other

        overlays, overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        overlay_entries = dict(overlays.get("entries") or {})
        previous_overlay = overlay_entries.get(paragraph_id)
        previous_overlay = previous_overlay if isinstance(previous_overlay, dict) else {}
        overlay_entries[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "source_text_sha256": str(
                previous_overlay.get("source_text_sha256")
                or self._text_sha256(str(job_payload["paragraph_text"]))
            ),
            "rewritten_text": str(job_payload["candidate_text"]).strip(),
            "updated_at": decided_at,
        }

        files: dict[
            str,
            tuple[bytes | Callable[[dict[str, ArtifactRecord]], bytes], str],
        ] = {
            DRAFT_DOCUMENT: ((candidate_draft.rstrip() + "\n").encode(), "markdown")
        }

        def quality_content(published: dict[str, ArtifactRecord]) -> bytes:
            quality_was_current = (
                current_quality.get("source_draft_artifact_id") == current.id
            )
            quality = {
                **updated_quality,
                # A targeted paragraph evaluation cannot make an already stale
                # full-draft evaluation current.  Preserve staleness unless the
                # source quality snapshot covered the complete current draft.
                "source_draft_artifact_id": (
                    published[DRAFT_DOCUMENT].id
                    if quality_was_current
                    else current_quality.get("source_draft_artifact_id")
                ),
            }
            return (json.dumps(quality, ensure_ascii=False, indent=2) + "\n").encode()

        files[DRAFT_QUALITY] = (quality_content, "json")
        files[DRAFT_OVERLAYS] = (
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "policy": "Apply only when paragraph_id and source_text_sha256 still match.",
                        "entries": overlay_entries,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode(),
            "json",
        )
        files[DRAFT_REWRITES] = (
            (
                json.dumps(
                    {"project_id": project_id, "entries": entries},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode(),
            "json",
        )
        expected_currents = {
            DRAFT_DOCUMENT: current.id,
            DRAFT_QUALITY: quality_artifact.id,
            DRAFT_REWRITES: store_artifact.id,
        }
        if overlay_artifact is not None:
            expected_currents[DRAFT_OVERLAYS] = overlay_artifact.id
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "rewrite-candidate",
            "subject_id": candidate_id,
            "decision": "accept",
            "details": {
                "paragraph_id": paragraph_id,
                "source_draft_artifact_id": current.id,
                "evaluation_scope": "single_paragraph",
                "paragraph_score_after": candidate.get("paragraph_score_after"),
                "overall_score_after": updated_quality["score"],
            },
            "created_at": utc_now(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    **dict(current.metadata),
                    "operation": "rewrite-accept-pre-evaluated-candidate",
                    "candidate_id": candidate_id,
                    "paragraph_id": paragraph_id,
                    "previous_draft_artifact_id": current.id,
                },
                approval_events=[event],
                expected_current_artifacts=expected_currents,
                invalidate_final=True,
            )
        return {
            "candidate_id": candidate_id,
            "decision": "accept",
            "draft_artifact_id": published[DRAFT_DOCUMENT].id,
            "quality_artifact_id": published[DRAFT_QUALITY].id,
            "paragraph_id": paragraph_id,
            "paragraph_score": dict(built.get("paragraph_score") or {}).get("score"),
            "score": updated_quality["score"],
            "evaluation_scope": "single_paragraph",
            "revision": state.revision,
        }

    def decide_rewrite(
        self,
        principal: Principal,
        project_id: str,
        candidate_id: str,
        *,
        decision: str,
        revision: int,
    ) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise WorkflowValidationError("Unknown rewrite decision.")
        store, store_artifact = self._read_json(principal, project_id, DRAFT_REWRITES)
        entries = dict(store.get("entries") or {})
        candidate = dict(entries.get(candidate_id) or {})
        if not candidate:
            raise WorkflowNotFound("Rewrite candidate not found.")
        if candidate.get("status") != "pending":
            raise WorkflowConflict("Rewrite candidate was already decided.")
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        current_quality = self._artifact(principal, project_id, DRAFT_QUALITY)
        overlays, overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        if candidate.get("source_draft_artifact_id") != current.id:
            raise WorkflowConflict("Rewrite candidate is stale for the current Draft.")
        if (
            current_quality is None
            or candidate.get("source_quality_artifact_id") != current_quality.id
        ):
            raise WorkflowConflict("Rewrite candidate is stale for the current evaluation.")
        files: dict[str, tuple[bytes, str]] = {}
        expected_currents = {
            DRAFT_DOCUMENT: current.id,
            DRAFT_QUALITY: current_quality.id,
            DRAFT_REWRITES: store_artifact.id,
        }
        if decision == "accept":
            paragraph = next(
                (
                    row
                    for row in self._paragraph_spans(current_text)
                    if row["paragraph_id"] == candidate.get("paragraph_id")
                ),
                None,
            )
            if paragraph is None or self._normalized(paragraph["text"]) != self._normalized(
                candidate.get("original_text")
            ):
                raise WorkflowConflict("Rewrite paragraph changed after candidate generation.")
            updated = (
                current_text[: paragraph["start"]]
                + str(candidate["candidate_text"]).strip()
                + current_text[paragraph["end"] :]
            )
            files[DRAFT_DOCUMENT] = ((updated.rstrip() + "\n").encode(), "markdown")
            overlay_entries = dict(overlays.get("entries") or {})
            previous_overlay = overlay_entries.get(candidate.get("paragraph_id"))
            previous_overlay = (
                previous_overlay if isinstance(previous_overlay, dict) else {}
            )
            paragraph_id = str(candidate.get("paragraph_id") or "")
            overlay_entries[paragraph_id] = {
                "paragraph_id": paragraph_id,
                "source_text_sha256": str(
                    previous_overlay.get("source_text_sha256")
                    or self._text_sha256(str(candidate.get("original_text") or ""))
                ),
                "rewritten_text": str(candidate["candidate_text"]).strip(),
                "updated_at": utc_now().isoformat(),
            }
            files[DRAFT_OVERLAYS] = (
                (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "project_id": project_id,
                            "policy": "Apply only when paragraph_id and source_text_sha256 still match.",
                            "entries": overlay_entries,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode(),
                "json",
            )
            if overlay_artifact is not None:
                expected_currents[DRAFT_OVERLAYS] = overlay_artifact.id
            for other_id, other_value in list(entries.items()):
                if other_id == candidate_id or not isinstance(other_value, dict):
                    continue
                other = dict(other_value)
                if (
                    other.get("status") == "pending"
                    and other.get("source_draft_artifact_id") == current.id
                ):
                    other["status"] = "superseded"
                    other["superseded_by_candidate_id"] = candidate_id
                    other["decided_at"] = utc_now().isoformat()
                    entries[other_id] = other
        candidate["status"] = "accepted" if decision == "accept" else "rejected"
        candidate["decided_at"] = utc_now().isoformat()
        entries[candidate_id] = candidate
        files[DRAFT_REWRITES] = (
            (
                json.dumps(
                    {"project_id": project_id, "entries": entries},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode(),
            "json",
        )
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "rewrite-candidate",
            "subject_id": candidate_id,
            "decision": decision,
            "details": {
                "paragraph_id": candidate.get("paragraph_id"),
                "source_draft_artifact_id": current.id,
            },
            "created_at": utc_now(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=revision,
                metadata={
                    **dict(current.metadata),
                    "operation": f"rewrite-{decision}",
                    "candidate_id": candidate_id,
                    "previous_draft_artifact_id": current.id,
                },
                approval_events=[event],
                expected_current_artifacts=expected_currents,
                invalidate_final=decision == "accept",
            )
        return {
            "candidate_id": candidate_id,
            "decision": decision,
            "draft_artifact_id": (
                published[DRAFT_DOCUMENT].id if DRAFT_DOCUMENT in published else current.id
            ),
            "revision": state.revision,
        }

    def approve(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        override_low_score: bool,
        override_reason: str,
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        quality = payload.get("quality") or {}
        if not quality.get("current"):
            raise DraftApprovalBlocked("Evaluate the exact current Draft before approval.")
        hard = [str(value) for value in quality.get("hard_gate_failures") or [] if str(value)]
        score = float(quality.get("score") or 0)
        goal = float(quality.get("goal") or 90)
        if hard:
            raise DraftApprovalBlocked(
                "The current evaluation has hard gate failures that cannot be overridden."
            )
        needs_human_override = score < goal
        if needs_human_override and not override_low_score:
            raise DraftApprovalBlocked(
                "The current evaluation has unresolved findings; human override is required."
            )
        if needs_human_override and not str(override_reason or "").strip():
            raise WorkflowValidationError("A reason is required for human override.")
        draft_id = str(payload["draft_artifact_id"])
        quality_artifact_id = str(payload.get("quality_artifact_id") or "")
        if not quality_artifact_id:
            raise DraftApprovalBlocked("The current evaluation artifact is unavailable.")
        approval = {
            "status": "approved",
            "draft_artifact_id": draft_id,
            "quality_artifact_id": quality_artifact_id,
            "score": score,
            "goal": goal,
            "below_goal_override": score < goal,
            "overridden_hard_gate_failures": [],
            "override_reason": str(override_reason or "").strip(),
            "approved_at": utc_now().isoformat(),
        }
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "draft-version",
            "subject_id": draft_id,
            "decision": "approved",
            "details": approval,
            "created_at": utc_now(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_APPROVAL: (
                        (json.dumps(approval, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                status="approved",
                metadata={"operation": "draft-approval", "draft_artifact_id": draft_id},
                approval_events=[event],
                expected_current_artifacts={
                    DRAFT_DOCUMENT: draft_id,
                    DRAFT_QUALITY: quality_artifact_id,
                },
                invalidate_final=False,
            )
        return {
            "approved": True,
            "approval_artifact_id": published[DRAFT_APPROVAL].id,
            "revision": state.revision,
            "next_stage": "final",
        }
