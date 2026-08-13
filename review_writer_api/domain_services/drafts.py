"""PostgreSQL-native draft assembly, editing, quality, rewrite, and approval."""

from __future__ import annotations

import json
import re
import threading
import uuid
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


DRAFT_DOCUMENT = "draft/manuscript.md"
DRAFT_QUALITY = "draft/quality.json"
DRAFT_REWRITES = "draft/rewrite-candidates.json"
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
        files: dict[str, tuple[bytes, str]],
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
        for index, (logical_name, (content, artifact_type)) in enumerate(files.items()):
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

    @staticmethod
    def _assemble_markdown(
        title: str,
        section_index: dict[str, Any],
        figure_manifest: dict[str, Any],
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
        citation_numbers: dict[str, int] = {}
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
                if callouts and not re.search(r"\[(?:\d+[\d,;\s-]*)\]", text):
                    text = f"{text} [{', '.join(str(value) for value in callouts)}]"
                parts.append(f"{text}\n\n<!-- paragraph_id: {paragraph_id} -->")
                for figure in figures_by_paragraph.get(paragraph_id, []):
                    figure_number += 1
                    output_id = str(figure["output_artifact_id"])
                    label = str(figure.get("source_label") or f"Figure {figure_number}")
                    metadata = json.dumps(
                        {
                            "figure_id": figure.get("figure_id"),
                            "target_paragraph_id": paragraph_id,
                            "output_artifact_id": output_id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    parts.append(
                        "\n".join(
                            (
                                f"<!-- inserted_figure: {metadata} -->",
                                f"![{label}](/api/v1/artifacts/{output_id}/content)",
                                f"*Figure {figure_number}. {label}*",
                            )
                        )
                    )
        if citation_numbers:
            references = ["## References"]
            references.extend(
                f"[{number}] {paper_id}"
                for paper_id, number in sorted(
                    citation_numbers.items(), key=lambda item: item[1]
                )
            )
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
        manifest, manifest_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST
        )
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        expected_revision = state.revision if state else 0
        project = self._owned_project(principal, project_id)
        markdown = self._assemble_markdown(
            str(project.topic or project.slug or project_id),
            sections,
            manifest,
        )
        with self._write_lock:
            published, next_state = self._publish_files(
                principal,
                project_id,
                {DRAFT_DOCUMENT: (markdown.encode("utf-8"), "markdown")},
                expected_revision=expected_revision,
                metadata={
                    "source_sections_artifact_id": sections_artifact.id,
                    "source_figure_manifest_artifact_id": manifest_artifact.id,
                    "operation": "assemble",
                },
                expected_current_artifacts={
                    SECTION_INDEX: sections_artifact.id,
                    FIGURE_MANIFEST: manifest_artifact.id,
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
        }

    def _freshness(
        self, principal: Principal, project_id: str, draft: ArtifactRecord | None
    ) -> dict[str, Any]:
        sections = self._artifact(principal, project_id, SECTION_INDEX)
        figures = self._artifact(principal, project_id, FIGURE_MANIFEST)
        metadata = dict(draft.metadata if draft else {})
        upstream_stale = bool(
            draft
            and (
                not sections
                or not figures
                or metadata.get("source_sections_artifact_id") != sections.id
                or metadata.get("source_figure_manifest_artifact_id") != figures.id
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
        approval, _approval_artifact = self._read_json(
            principal, project_id, DRAFT_APPROVAL, required=False
        )
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        paragraphs = self._paragraph_spans(text)
        paragraph_by_id = {row["paragraph_id"]: row for row in paragraphs}
        manifest, _manifest_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        images_by_paragraph: dict[str, list[dict[str, str]]] = {}
        for row in manifest.get("figures") or []:
            if not isinstance(row, dict):
                continue
            paragraph_id = str(row.get("target_paragraph_id") or "")
            output_id = str(row.get("output_artifact_id") or "")
            if paragraph_id and output_id:
                images_by_paragraph.setdefault(paragraph_id, []).append(
                    {
                        "figure_id": str(row.get("figure_id") or ""),
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
        jobs = self.repository.list_project_jobs(principal.user_id, project_id)
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
        return {
            "project_id": project_id,
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "draft_artifact_id": draft_artifact.id if draft_artifact else "",
            "first_draft_md": text,
            "paragraphs": [
                {key: value for key, value in row.items() if key not in {"start", "end", "marker_end"}}
                for row in paragraphs
            ],
            "quality": public_quality,
            "quality_artifact_id": quality_artifact.id if quality_artifact else "",
            "rewrite_candidates": rewrite_candidates,
            "rewrite_states": rewrite_states,
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
        self, principal: Principal, project_id: str, *, goal: float
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        if not payload["draft_artifact_id"]:
            raise DraftNotReady("Assemble and save Draft before evaluation.")
        if payload["freshness"]["upstream_stale"]:
            raise DraftNotReady("Draft inputs changed. Reassemble Draft before evaluation.")
        return {
            **self.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "expected_revision": payload["revision"],
            "draft_text": payload["first_draft_md"],
            "paragraphs": payload["paragraphs"],
            "goal": max(0.0, min(float(goal), 100.0)),
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
            "revision": state.revision,
        }

    def rewrite_payload(
        self, principal: Principal, project_id: str, paragraph_id: str
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        quality = payload.get("quality") or {}
        if not quality.get("current"):
            raise DraftNotReady(
                "Evaluate the exact current Draft before requesting a rewrite."
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
        return {
            **self.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "paragraph_id": paragraph_id,
            "paragraph_text": paragraph["text"],
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "expected_revision": payload["revision"],
            "quality": quality,
            "issues": matching_issues,
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
            "resolved_issue_ids": list(built.get("resolved_issue_ids") or []),
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
        if hard:
            raise DraftApprovalBlocked("Resolve hard integrity failures before approval.")
        score = float(quality.get("score") or 0)
        goal = float(quality.get("goal") or 90)
        if score < goal and not override_low_score:
            raise DraftApprovalBlocked(
                f"The current score {score:.1f} is below the target {goal:.1f}; human override is required."
            )
        if score < goal and not str(override_reason or "").strip():
            raise WorkflowValidationError("A reason is required for low-score approval.")
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
