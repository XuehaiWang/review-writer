"""PostgreSQL-native conclusion, overview, final build, validation, and export."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import UnidentifiedImageError
from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import database_session, utc_now
from review_writer_api.domain_services.drafts import (
    DRAFT_APPROVAL,
    DRAFT_DOCUMENT,
    DRAFT_QUALITY,
    DraftsService,
)
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.figure_rules import image_size
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.latex_renderer import SUPPORTED_PROFILES, TEMPLATE_VERSION
from review_writer_core.manuscript_state import build_manuscript_state
from review_writer_core.publication_voice import publication_voice_issues


FINAL_CONCLUSION = "final/conclusion.md"
FINAL_CONCLUSION_REPORT = "final/conclusion-report.json"
FINAL_OVERVIEW_IMAGE = "final/overview.png"
FINAL_OVERVIEW_TEXT = "final/overview-text.json"
FINAL_DRAFT = "final/manuscript.md"
FINAL_VALIDATION = "final/validation.json"
FINAL_RELEASE = "final/release.json"
FINAL_DOCX = "final/manuscript.docx"
FINAL_DOCX_QA = "final/docx-qa.json"
FINAL_MANUSCRIPT_STATE = "final/manuscript_state.json"
FINAL_RENDER_MANIFEST = "final/render_manifest.json"
FINAL_TEX = "final/manuscript.tex"
FINAL_PDF = "final/manuscript.pdf"
FINAL_PDF_QA = "final/pdf-qa.json"
FINAL_PDF_COMPILE_LOG = "final/pdf-compile.log"
ARTIFACT_URL = re.compile(r"/api/v1/artifacts/([0-9a-fA-F-]{36})/content")
REFERENCES_HEADING = re.compile(
    r"(?im)^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$"
)
CITATION_CALLOUT = re.compile(r"\[([0-9][0-9,;\s-]*)\]")
REFERENCE_ITEM = re.compile(r"(?m)^\s*\[(\d+)\]\s*\.?\s+(.+?)\s*$")
MARKDOWN_HEADING = re.compile(r"(?m)^\s*(#{1,6})\s+(.+?)\s*$")
INTRODUCTION_TITLES = ("introduction", "background", "引言", "绪论", "研究背景")
REFERENCE_AFFILIATION_SUP = re.compile(
    r"<sup\b[^>]*>[\s,;:.·•*†‡#\[\](){}\-]*</sup>",
    re.IGNORECASE,
)
INSERTED_FIGURE_METADATA = re.compile(
    r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.DOTALL
)


def _clean_reference_affiliation_markup(markdown: str) -> str:
    """Remove empty author-affiliation superscripts without touching scientific markup."""

    reference_match = REFERENCES_HEADING.search(markdown or "")
    if reference_match is None:
        return markdown
    reference_text = markdown[reference_match.start() :]
    cleaned = REFERENCE_AFFILIATION_SUP.sub("", reference_text)
    cleaned = "\n".join(
        re.sub(r"[ \t]{2,}", " ", line) for line in cleaned.split("\n")
    )
    return markdown[: reference_match.start()] + cleaned


def _figure_argument_findings(markdown: str) -> list[dict[str, Any]]:
    """Check reader-visible figure closure without treating hidden metadata as a callout."""

    findings: list[dict[str, Any]] = []
    for match in INSERTED_FIGURE_METADATA.finditer(markdown or ""):
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError:
            findings.append({"figure_id": "", "issues": ["invalid_inserted_figure_metadata"]})
            continue
        if not isinstance(metadata, dict):
            continue
        figure_id = str(metadata.get("figure_id") or "")
        published_label = str(metadata.get("published_label") or "").strip()
        output_artifact_id = str(metadata.get("output_artifact_id") or "")
        issues: list[str] = []
        if not published_label:
            issues.append("published_label_missing")
        escaped_label = re.escape(published_label)
        if output_artifact_id and not re.search(
            rf"!\[[^\]]*\]\(/api/v1/artifacts/{re.escape(output_artifact_id)}/content\)",
            markdown,
        ):
            issues.append("image_missing")
        if published_label and not re.search(rf"\*{escaped_label}\.", markdown):
            issues.append("caption_missing")
        visible_prose = INSERTED_FIGURE_METADATA.sub("", markdown)
        visible_prose = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", visible_prose)
        visible_prose = re.sub(r"(?m)^\s*\*Figure\s+\d+\..*?\*\s*$", "", visible_prose)
        if published_label and not re.search(
            rf"\b{escaped_label}\b[^\n]{{0,500}}\b(?:presents?|shows?|summarizes?|illustrates?|compares?|depicts?)\b",
            visible_prose,
            re.IGNORECASE,
        ):
            issues.append("visible_callout_or_interpretation_missing")
        if not str(metadata.get("paper_id") or "").strip():
            issues.append("source_paper_identity_missing")
        if str(metadata.get("interpretation_basis") or "") != "source_caption":
            issues.append("paper_level_interpretation_missing")
        if issues:
            findings.append(
                {
                    "figure_id": figure_id,
                    "published_label": published_label,
                    "paper_id": str(metadata.get("paper_id") or ""),
                    "issues": issues,
                }
            )
    return findings


class FinalNotReady(WorkflowConflict):
    code = "FINAL_NOT_READY"


class FinalService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        drafts: DraftsService,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.drafts = drafts
        self._write_lock = threading.RLock()

    def _artifact(self, principal: Principal, project_id: str, logical_name: str):
        principal.require(Permission.PROJECT_READ)
        if self.repository.get_owned_project(principal.user_id, project_id) is None:
            raise WorkflowNotFound("Project not found.")
        return self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )

    def _read_text(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = False,
    ) -> tuple[str, ArtifactRecord | None]:
        artifact = self._artifact(principal, project_id, logical_name)
        if artifact is None:
            if required:
                raise WorkflowNotFound("Current workflow artifact not found.")
            return "", None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        return resolved.path.read_text(encoding="utf-8"), artifact

    def _read_json(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
    ) -> tuple[dict[str, Any], ArtifactRecord | None]:
        text, artifact = self._read_text(principal, project_id, logical_name)
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
        metadata: dict[str, Any],
        status: str = "review",
        expected_current_artifacts: dict[str, str] | None = None,
        expected_stage_states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            "final",
            status="succeeded",
            input_snapshot=metadata,
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
                producer_stage="final",
                make_current=False,
                metadata=metadata,
            )
        state = self.repository.promote_stage_artifacts_atomically(
            principal.user_id,
            project_id,
            "final",
            artifact_ids={name: record.id for name, record in published.items()},
            run_id=run.id,
            expected_revision=expected_revision,
            status=status,
            expected_current_artifacts=expected_current_artifacts,
            expected_stage_states=expected_stage_states,
        )
        return published, state

    def _approved_draft(
        self, principal: Principal, project_id: str
    ) -> tuple[str, ArtifactRecord, dict[str, Any]]:
        draft_payload = self.drafts.get(principal, project_id)
        if not draft_payload.get("draft_approval_current"):
            raise FinalNotReady("Human-approve the exact current Draft before Final work.")
        text, artifact = self._read_text(
            principal, project_id, DRAFT_DOCUMENT, required=True
        )
        approval, _approval_artifact = self._read_json(
            principal, project_id, DRAFT_APPROVAL
        )
        if approval.get("draft_artifact_id") != artifact.id:
            raise FinalNotReady("Draft approval is stale.")
        return text, artifact, approval

    @staticmethod
    def _insert_before_introduction(markdown: str, block: str) -> str:
        """Place a front-of-article artifact immediately before Introduction."""

        normalized_block = str(block or "").strip()
        body = str(markdown or "").rstrip()
        if not normalized_block:
            return body
        headings = list(MARKDOWN_HEADING.finditer(body))
        insertion: int | None = None
        for match in headings:
            title = re.sub(
                r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", match.group(2)
            ).strip().casefold()
            if any(
                title == candidate
                or any(
                    title.startswith(f"{candidate}{separator}")
                    for separator in (" ", ":", "：", "与", "和")
                )
                for candidate in INTRODUCTION_TITLES
            ):
                insertion = match.start()
                break
        if insertion is None and headings and len(headings[0].group(1)) == 1:
            insertion = headings[0].end()
        if insertion is None:
            insertion = 0
        before = body[:insertion].rstrip()
        after = body[insertion:].lstrip()
        return "\n\n".join(
            value for value in (before, normalized_block, after) if value
        )

    def _revision(self, principal: Principal, project_id: str) -> int:
        state = self.repository.get_stage_state(principal.user_id, project_id, "final")
        return state.revision if state else 0

    def conclusion_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        text, draft, _approval = self._approved_draft(principal, project_id)
        return {
            **self.drafts.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "draft_text": text,
            "source_draft_artifact_id": draft.id,
            "expected_revision": self._revision(principal, project_id),
        }

    def build_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        _text, draft, _approval = self._approved_draft(principal, project_id)
        return {
            "project_id": project_id,
            "source_draft_artifact_id": draft.id,
        }

    def publish_conclusion(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        _text, current, _approval = self._approved_draft(principal, project_id)
        if current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while conclusion was generated.")
        markdown = str(built.get("markdown") or "").strip()
        if not markdown:
            raise WorkflowValidationError("Conclusion generation returned no Markdown.")
        report = built.get("report")
        report = report if isinstance(report, dict) else {}
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_CONCLUSION: ((markdown + "\n").encode(), "markdown"),
                    FINAL_CONCLUSION_REPORT: (
                        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "conclusion",
                    "source_draft_artifact_id": current.id,
                },
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
            )
        return {
            "conclusion_artifact_id": published[FINAL_CONCLUSION].id,
            "conclusion_report_artifact_id": published[FINAL_CONCLUSION_REPORT].id,
            "revision": state.revision,
        }

    def overview_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        text, draft, _approval = self._approved_draft(principal, project_id)
        return {
            **self.drafts.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "draft_text": text,
            "source_draft_artifact_id": draft.id,
            "expected_revision": self._revision(principal, project_id),
        }

    def publish_overview(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        _text, current, _approval = self._approved_draft(principal, project_id)
        if current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while overview was generated.")
        raw_output = str(built.get("output_path") or "").strip()
        output = Path(raw_output).resolve() if raw_output else None
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        try:
            if output is None:
                raise ValueError
            output.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError("Overview output escaped its user workspace.") from exc
        if output.is_symlink() or not output.is_file():
            raise WorkflowValidationError("Overview generation produced no image.")
        try:
            image_size(output)
        except (OSError, UnidentifiedImageError) as exc:
            raise WorkflowValidationError("Overview image is unreadable.") from exc
        editable = built.get("editable_text")
        if not isinstance(editable, dict) or not str(editable.get("title") or "").strip():
            raise WorkflowValidationError("Overview generation returned no editable text model.")
        files = {
            FINAL_OVERVIEW_IMAGE: (output.read_bytes(), output.suffix.lstrip(".") or "png"),
            FINAL_OVERVIEW_TEXT: (
                (json.dumps(editable, ensure_ascii=False, indent=2) + "\n").encode(),
                "json",
            ),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "overview",
                    "source_draft_artifact_id": current.id,
                    "report": dict(built.get("report") or {}),
                },
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
            )
        return {
            "overview_artifact_id": published[FINAL_OVERVIEW_IMAGE].id,
            "overview_text_artifact_id": published[FINAL_OVERVIEW_TEXT].id,
            "revision": state.revision,
        }

    def save_overview_text(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        title: str,
        subtitle: str,
        labels: list[str],
    ) -> dict[str, Any]:
        _draft_text, draft, _approval = self._approved_draft(principal, project_id)
        current_value, current = self._read_json(
            principal, project_id, FINAL_OVERVIEW_TEXT
        )
        if current is None:
            raise FinalNotReady("Generate the overview before editing its text.")
        overview = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        if (
            overview is None
            or current.metadata.get("source_draft_artifact_id") != draft.id
            or overview.metadata.get("source_draft_artifact_id") != draft.id
        ):
            raise FinalNotReady(
                "The overview belongs to an older Draft. Generate it again before editing."
            )
        requested = {
            "title": str(title).strip(),
            "subtitle": str(subtitle).strip(),
            "labels": [str(value).strip() for value in labels if str(value).strip()],
        }
        if all(current_value.get(key) == value for key, value in requested.items()):
            raise WorkflowValidationError("Overview text has no change.")
        edited = {
            **current_value,
            **requested,
            "edited_at": utc_now().isoformat(),
        }
        metadata = {**dict(current.metadata), "operation": "overview-text-edit"}
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_OVERVIEW_TEXT: (
                        (json.dumps(edited, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                metadata=metadata,
                expected_current_artifacts={FINAL_OVERVIEW_TEXT: current.id},
            )
        return {
            "overview_text_artifact_id": published[FINAL_OVERVIEW_TEXT].id,
            "revision": state.revision,
        }

    def _validate_markdown(
        self,
        principal: Principal,
        project_id: str,
        markdown: str,
        *,
        source_paper_ids: list[str],
        source_reference_numbers: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        missing: list[str] = []
        wrong_project: list[str] = []
        referenced = list(dict.fromkeys(ARTIFACT_URL.findall(markdown or "")))
        for artifact_id in referenced:
            try:
                resolved = self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact_id
                )
                if resolved.artifact.project_id != project_id:
                    wrong_project.append(artifact_id)
            except (WorkflowNotFound, WorkflowConflict):
                missing.append(artifact_id)
        reference_match = REFERENCES_HEADING.search(markdown or "")
        body = markdown[: reference_match.start()] if reference_match else markdown
        reference_text = markdown[reference_match.end() :] if reference_match else ""

        def numbers(text: str) -> set[int]:
            values: set[int] = set()
            for match in CITATION_CALLOUT.finditer(text):
                for start, end in re.findall(r"(\d+)\s*-\s*(\d+)", match.group(1)):
                    lower, upper = sorted((int(start), int(end)))
                    values.update(range(lower, upper + 1))
                for token in re.findall(r"\d+", match.group(1)):
                    values.add(int(token))
            return values

        callouts = numbers(body)
        listed_rows = REFERENCE_ITEM.findall(reference_text)
        listed = {int(number) for number, _text in listed_rows}
        normalized_sources = list(
            dict.fromkeys(
                str(value).strip()
                for value in source_paper_ids
                if str(value).strip()
            )
        )
        reference_numbers = {
            paper_id: int(number)
            for paper_id, number in (source_reference_numbers or {}).items()
            if paper_id in normalized_sources and int(number) > 0
        }
        for index, paper_id in enumerate(normalized_sources, start=1):
            reference_numbers.setdefault(paper_id, index)
        missing_sources = [
            paper_id
            for paper_id in normalized_sources
            if reference_numbers[paper_id] not in listed
        ]
        listed_source_ids: list[str] = []
        unmapped_reference_numbers: list[int] = []
        sources_by_number = {
            number: paper_id for paper_id, number in reference_numbers.items()
        }
        for number, _text in listed_rows:
            matched_paper_id = sources_by_number.get(int(number))
            if matched_paper_id:
                listed_source_ids.append(matched_paper_id)
            else:
                unmapped_reference_numbers.append(int(number))
        active_sources, immutable_sources = self._library_source_sets(
            principal, normalized_sources
        )
        unavailable_sources = sorted(set(normalized_sources) - active_sources)
        missing_source_artifacts = sorted(
            set(normalized_sources) - immutable_sources
        )
        blocking_issues: list[str] = []
        if missing:
            blocking_issues.append("missing_artifact_references")
        if wrong_project:
            blocking_issues.append("cross_project_artifact_references")
        warning_issues: list[str] = []
        if not reference_match:
            warning_issues.append("missing_references_section")
        elif not listed:
            warning_issues.append("empty_references_section")
        if normalized_sources and not callouts:
            warning_issues.append("draft_has_no_citation_callouts")
        if callouts != listed:
            warning_issues.append("citation_reference_map_mismatch")
        if missing_sources:
            warning_issues.append("citation_sources_missing_from_references")
        if unmapped_reference_numbers:
            warning_issues.append("references_include_unmapped_sources")
        if unavailable_sources:
            warning_issues.append("library_sources_unavailable")
        if missing_source_artifacts:
            warning_issues.append("library_source_artifacts_missing")
        voice_issues = publication_voice_issues(body)
        if voice_issues:
            warning_issues.append("publication_voice_leakage")
        figure_argument_findings = _figure_argument_findings(body)
        if figure_argument_findings:
            warning_issues.append("figure_argument_closure_incomplete")
        return {
            "valid": not blocking_issues,
            "referenced_artifact_ids": referenced,
            "missing_artifact_ids": missing,
            "cross_project_artifact_ids": wrong_project,
            "citation_callouts": sorted(callouts),
            "listed_references": sorted(listed),
            "source_paper_ids": normalized_sources,
            "missing_source_paper_ids": missing_sources,
            "listed_source_paper_ids": list(dict.fromkeys(listed_source_ids)),
            "unmapped_reference_numbers": sorted(unmapped_reference_numbers),
            "unavailable_source_paper_ids": unavailable_sources,
            "missing_source_artifact_paper_ids": missing_source_artifacts,
            "publication_voice_issues": voice_issues,
            "figure_argument_findings": figure_argument_findings,
            "references_section_present": bool(reference_match),
            "blocking_issues": blocking_issues,
            "warning_issues": warning_issues,
            "validated_at": utc_now().isoformat(),
        }

    def _library_source_sets(
        self, principal: Principal, paper_ids: list[str]
    ) -> tuple[set[str], set[str]]:
        normalized = tuple(
            dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip())
        )
        if not normalized:
            return set(), set()
        with database_session(self.repository.session_factory) as session:
            active = set(
                session.scalars(
                    select(LibraryPaper.paper_id).where(
                        LibraryPaper.user_id == uuid.UUID(principal.user_id),
                        LibraryPaper.paper_id.in_(normalized),
                        LibraryPaper.status == "active",
                        LibraryPaper.deleted_at.is_(None),
                    )
                ).all()
            )
            available = set(
                session.scalars(
                    select(LibraryArtifact.paper_id)
                    .where(
                        LibraryArtifact.user_id == uuid.UUID(principal.user_id),
                        LibraryArtifact.paper_id.in_(normalized),
                        LibraryArtifact.availability == "available",
                    )
                    .distinct()
                ).all()
            )
        return active, available

    def build(self, principal: Principal, project_id: str) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        draft_text, draft, approval = self._approved_draft(principal, project_id)
        draft_state = self.repository.get_stage_state(
            principal.user_id, project_id, "draft"
        )
        approval_artifact = self._artifact(principal, project_id, DRAFT_APPROVAL)
        quality_artifact = self._artifact(principal, project_id, DRAFT_QUALITY)
        if (
            draft_state is None
            or draft_state.status != "approved"
            or approval_artifact is None
            or quality_artifact is None
            or approval.get("quality_artifact_id") != quality_artifact.id
        ):
            raise FinalNotReady("Draft approval or evaluation changed before Final build.")
        conclusion, conclusion_artifact = self._read_text(
            principal, project_id, FINAL_CONCLUSION
        )
        conclusion_report, conclusion_report_artifact = self._read_json(
            principal, project_id, FINAL_CONCLUSION_REPORT
        )
        overview = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        overview_text, overview_text_artifact = self._read_json(
            principal, project_id, FINAL_OVERVIEW_TEXT
        )
        if conclusion_artifact and conclusion_artifact.metadata.get(
            "source_draft_artifact_id"
        ) != draft.id:
            raise FinalNotReady(
                "The conclusion belongs to an older Draft. Generate it again or remove it."
            )
        if bool(conclusion_artifact) != bool(conclusion_report_artifact):
            raise FinalNotReady("The conclusion and its quality report are incomplete.")
        if bool(overview) != bool(overview_text_artifact):
            raise FinalNotReady("The overview image and editable text are incomplete.")
        if overview and (
            overview.metadata.get("source_draft_artifact_id") != draft.id
            or overview_text_artifact.metadata.get("source_draft_artifact_id") != draft.id
        ):
            raise FinalNotReady(
                "The overview belongs to an older Draft. Generate it again or remove it."
            )
        reference_match = REFERENCES_HEADING.search(draft_text)
        draft_body = (
            draft_text[: reference_match.start()].rstrip()
            if reference_match
            else draft_text.rstrip()
        )
        draft_references = (
            draft_text[reference_match.start() :].strip() if reference_match else ""
        )
        overview_block = ""
        if overview is not None:
            overview_lines = [
                f"![Overview figure](/api/v1/artifacts/{overview.id}/content)",
            ]
            caption = ". ".join(
                value
                for value in (
                    str(overview_text.get("title") or "").strip(),
                    str(overview_text.get("subtitle") or "").strip(),
                )
                if value
            )
            labels = [
                str(value).strip()
                for value in overview_text.get("labels") or []
                if str(value).strip()
            ]
            if labels:
                caption = (caption + " — " if caption else "") + ", ".join(labels)
            if caption:
                overview_lines.append(f"*Review overview. {caption}*")
            else:
                overview_lines.append("*Review overview.*")
            overview_block = "\n".join(overview_lines)
        assembled_body = self._insert_before_introduction(draft_body, overview_block)
        parts = [assembled_body]
        if conclusion:
            parts.append(conclusion.strip())
        if draft_references:
            parts.append(draft_references)
        markdown = "\n\n".join(parts).rstrip() + "\n"
        markdown = _clean_reference_affiliation_markup(markdown)
        compatibility = self.drafts.compatibility_payload(principal, project_id)
        source_paper_ids = [
            str(paper_id)
            for section in compatibility.get("section_index", {}).get("sections") or []
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict)
            for paper_id in (
                paragraph.get("cited_paper_ids")
                or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
            )
            if str(paper_id).strip()
        ]
        matrix_reference_numbers = {
            str(row.get("paper_id") or ""): index
            for index, row in enumerate(
                compatibility.get("matrix", {}).get("rows") or [], start=1
            )
            if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
        }
        validation = self._validate_markdown(
            principal,
            project_id,
            markdown,
            source_paper_ids=source_paper_ids,
            source_reference_numbers=matrix_reference_numbers,
        )
        if not validation["valid"]:
            raise FinalNotReady(
                "Final manuscript contains a missing or cross-project artifact reference."
            )
        release = {
            "status": "released",
            "source_draft_artifact_id": draft.id,
            "source_paper_ids": validation["source_paper_ids"],
            "validation_blocking_issues": [],
            "validation_warning_issues": validation["warning_issues"],
            "released_at": utc_now().isoformat(),
        }
        source_ids = {
            "source_draft_artifact_id": draft.id,
            "conclusion_artifact_id": conclusion_artifact.id if conclusion_artifact else "",
            "conclusion_report_artifact_id": (
                conclusion_report_artifact.id if conclusion_report_artifact else ""
            ),
            "overview_artifact_id": overview.id if overview else "",
            "overview_text_artifact_id": (
                overview_text_artifact.id if overview_text_artifact else ""
            ),
        }
        expected_currents = {
            DRAFT_DOCUMENT: draft.id,
            DRAFT_APPROVAL: approval_artifact.id,
            DRAFT_QUALITY: quality_artifact.id,
        }
        if conclusion_artifact:
            expected_currents[FINAL_CONCLUSION] = conclusion_artifact.id
        if conclusion_report_artifact:
            expected_currents[FINAL_CONCLUSION_REPORT] = conclusion_report_artifact.id
        if overview:
            expected_currents[FINAL_OVERVIEW_IMAGE] = overview.id
        if overview_text_artifact:
            expected_currents[FINAL_OVERVIEW_TEXT] = overview_text_artifact.id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_DRAFT: (markdown.encode(), "markdown"),
                    FINAL_VALIDATION: (
                        (json.dumps(validation, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_RELEASE: (
                        (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                },
                expected_revision=self._revision(principal, project_id),
                metadata={"operation": "final-build", **source_ids},
                expected_current_artifacts=expected_currents,
                expected_stage_states={
                    "draft": {
                        "revision": draft_state.revision,
                        "status": "approved",
                    }
                },
            )
        return {
            "final_artifact_id": published[FINAL_DRAFT].id,
            "validation_artifact_id": published[FINAL_VALIDATION].id,
            "release_artifact_id": published[FINAL_RELEASE].id,
            "revision": state.revision,
        }

    def export_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        current_payload = self.get(principal, project_id)
        if (
            not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise FinalNotReady("Build the current Final manuscript before Word export.")
        _draft_text, draft, _approval = self._approved_draft(principal, project_id)
        final_text, final_artifact = self._read_text(
            principal, project_id, FINAL_DRAFT, required=True
        )
        compatibility = self.drafts.compatibility_payload(principal, project_id)
        artifact_paths = dict(compatibility.get("figure_artifact_paths") or {})
        for artifact_id in dict.fromkeys(ARTIFACT_URL.findall(final_text)):
            artifact_paths[artifact_id] = str(
                self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact_id
                ).path
            )
        compatibility["figure_artifact_paths"] = artifact_paths
        return {
            **compatibility,
            "project_id": project_id,
            "source_draft_artifact_id": draft.id,
            "source_final_artifact_id": final_artifact.id,
            "final_markdown": final_text,
            "expected_revision": self._revision(principal, project_id),
            "source_release_artifact_id": current_payload["release_artifact_id"],
            "source_paper_ids": list(
                (current_payload.get("release") or {}).get("source_paper_ids") or []
            ),
        }

    def publish_export(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        final = self._artifact(principal, project_id, FINAL_DRAFT)
        current_payload = self.get(principal, project_id)
        if (
            final is None
            or final.id != job_payload["source_final_artifact_id"]
            or not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise WorkflowConflict("Final manuscript changed while DOCX was generated.")
        raw = str(built.get("output_path") or "").strip()
        output = Path(raw).resolve() if raw else None
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        try:
            if output is None:
                raise ValueError
            output.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError("DOCX output escaped its user workspace.") from exc
        if output.is_symlink() or not output.is_file() or output.suffix.casefold() != ".docx":
            raise WorkflowValidationError("DOCX export produced no document.")
        try:
            with zipfile.ZipFile(output) as archive:
                corrupt = archive.testzip()
                names = set(archive.namelist())
                required = {"[Content_Types].xml", "word/document.xml"}
                missing_parts = sorted(required - names)
                document = ET.fromstring(archive.read("word/document.xml"))
                namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraph_count = len(document.findall(".//w:p", namespace))
                table_count = len(document.findall(".//w:tbl", namespace))
                image_count = sum(name.startswith("word/media/") for name in names)
        except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise WorkflowValidationError(
                f"DOCX structural QA could not read the generated package: {type(exc).__name__}."
            ) from exc
        blockers = []
        if corrupt:
            blockers.append({"type": "corrupt_zip_member", "member": corrupt})
        if missing_parts:
            blockers.append({"type": "missing_docx_parts", "parts": missing_parts})
        if paragraph_count <= 0:
            blockers.append({"type": "empty_document_body"})
        docx_qa = {
            "schema_version": 1,
            "status": "blocked" if blockers else "pass",
            "paragraph_count": paragraph_count,
            "table_count": table_count,
            "image_count": image_count,
            "blocking_issues": blockers,
            "warning_issues": [],
            "checked_at": utc_now().isoformat(),
        }
        if blockers:
            raise WorkflowValidationError("DOCX failed structural package QA.")
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_DOCX: (output.read_bytes(), "docx"),
                    FINAL_DOCX_QA: (
                        (json.dumps(docx_qa, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "docx-export",
                    "source_final_artifact_id": final.id,
                    "source_release_artifact_id": job_payload[
                        "source_release_artifact_id"
                    ],
                    "download_name": str(built.get("download_name") or "review.docx"),
                },
                status="completed",
                expected_current_artifacts={
                    FINAL_DRAFT: final.id,
                    FINAL_RELEASE: job_payload["source_release_artifact_id"],
                },
            )
        return {
            "docx_artifact_id": published[FINAL_DOCX].id,
            "docx_qa_artifact_id": published[FINAL_DOCX_QA].id,
            "docx_qa": docx_qa,
            "download_name": str(built.get("download_name") or "review.docx"),
            "revision": state.revision,
        }

    def pdf_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        language_profile: str,
    ) -> dict[str, Any]:
        profile = str(language_profile or "").strip()
        if profile not in SUPPORTED_PROFILES:
            raise WorkflowValidationError(
                "PDF language profile must be `en` or `zh-CN`."
            )
        current_payload = self.get(principal, project_id)
        if (
            not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise FinalNotReady("Build the current Final manuscript before PDF export.")
        _draft_text, draft, _approval = self._approved_draft(principal, project_id)
        final_text, final_artifact = self._read_text(
            principal, project_id, FINAL_DRAFT, required=True
        )
        compatibility = self.drafts.compatibility_payload(principal, project_id)
        # The PDF worker receives only assets that the released manuscript
        # actually references. This keeps the isolated request bounded without
        # changing the upstream Images approval/redraw workflow.
        artifact_paths: dict[str, str] = {}
        for artifact_id in dict.fromkeys(ARTIFACT_URL.findall(final_text)):
            artifact_paths[artifact_id] = str(
                self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact_id
                ).path
            )
        return {
            **compatibility,
            "project_id": project_id,
            "source_draft_artifact_id": draft.id,
            "source_final_artifact_id": final_artifact.id,
            "source_release_artifact_id": current_payload["release_artifact_id"],
            "final_markdown": final_text,
            "figure_artifact_paths": artifact_paths,
            "language_profile": profile,
            "template": "modern-survey",
            "expected_revision": self._revision(principal, project_id),
        }

    @staticmethod
    def _trusted_generated_file(
        raw: Any,
        *,
        user_root: Path,
        suffix: str,
        label: str,
    ) -> Path:
        value = str(raw or "").strip()
        path = Path(value).resolve() if value else None
        try:
            if path is None:
                raise ValueError
            path.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                f"{label} output escaped its user workspace."
            ) from exc
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() != suffix:
            raise WorkflowValidationError(f"{label} export produced no valid output.")
        return path

    def publish_pdf(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        final = self._artifact(principal, project_id, FINAL_DRAFT)
        current_payload = self.get(principal, project_id)
        if (
            final is None
            or final.id != job_payload["source_final_artifact_id"]
            or current_payload.get("release_artifact_id")
            != job_payload["source_release_artifact_id"]
            or not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise WorkflowConflict("Final manuscript changed while PDF was generated.")
        profile = str(job_payload.get("language_profile") or "")
        if profile not in SUPPORTED_PROFILES:
            raise WorkflowValidationError("PDF language profile is invalid.")
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        pdf_path = self._trusted_generated_file(
            built.get("output_path"),
            user_root=user_root,
            suffix=".pdf",
            label="PDF",
        )
        tex_path = self._trusted_generated_file(
            built.get("tex_path"),
            user_root=user_root,
            suffix=".tex",
            label="LaTeX",
        )
        log_path = self._trusted_generated_file(
            built.get("compile_log_path"),
            user_root=user_root,
            suffix=".log",
            label="PDF compile log",
        )
        state_payload = built.get("manuscript_state")
        manifest = built.get("render_manifest")
        qa = built.get("pdf_qa")
        if not all(isinstance(item, dict) for item in (state_payload, manifest, qa)):
            raise WorkflowValidationError("PDF renderer returned an incomplete state bundle.")
        if (
            manifest.get("language_profile") != profile
            or manifest.get("template") != "modern-survey"
            or manifest.get("template_version") != TEMPLATE_VERSION
            or manifest.get("source_final_artifact_id") != final.id
            or manifest.get("source_release_artifact_id")
            != job_payload["source_release_artifact_id"]
            or manifest.get("shell_escape") is not False
        ):
            raise WorkflowValidationError("PDF render manifest does not match the current job.")
        if (
            not state_payload.get("validation", {}).get("valid")
            or qa.get("status") not in {"pass", "pass_with_warnings"}
            or qa.get("blocking_issues")
            or not qa.get("all_fonts_embedded")
        ):
            raise WorkflowValidationError(
                "PDF failed deterministic content or visual publication gates."
            )
        expected_state = build_manuscript_state(
            str(job_payload.get("final_markdown") or ""),
            artifact_paths=dict(job_payload.get("figure_artifact_paths") or {}),
        )
        if (
            state_payload.get("source_markdown_sha256")
            != expected_state.get("source_markdown_sha256")
            or state_payload.get("semantic_sha256")
            != expected_state.get("semantic_sha256")
            or state_payload.get("counts") != expected_state.get("counts")
        ):
            raise WorkflowValidationError(
                "PDF Final Manuscript State diverged from the released Markdown."
            )
        if (
            manifest.get("source_markdown_sha256")
            != state_payload.get("source_markdown_sha256")
            or manifest.get("semantic_sha256")
            != state_payload.get("semantic_sha256")
            or set(dict(manifest.get("asset_sha256") or {}))
            != set(dict(job_payload.get("figure_artifact_paths") or {}))
        ):
            raise WorkflowValidationError(
                "PDF render manifest diverged from its manuscript or approved assets."
            )
        metadata = {
            "operation": "pdf-export",
            "source_final_artifact_id": final.id,
            "source_release_artifact_id": job_payload["source_release_artifact_id"],
            "language_profile": profile,
            "template": "modern-survey",
            "download_name": str(
                built.get("download_name") or f"review.{profile}.pdf"
            ),
        }
        with self._write_lock:
            published, stage = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_MANUSCRIPT_STATE: (
                        (json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_RENDER_MANIFEST: (
                        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_TEX: (tex_path.read_bytes(), "tex"),
                    FINAL_PDF: (pdf_path.read_bytes(), "pdf"),
                    FINAL_PDF_QA: (
                        (json.dumps(qa, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_PDF_COMPILE_LOG: (log_path.read_bytes(), "log"),
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata=metadata,
                status="completed",
                expected_current_artifacts={
                    FINAL_DRAFT: final.id,
                    FINAL_RELEASE: job_payload["source_release_artifact_id"],
                },
            )
        return {
            "pdf_artifact_id": published[FINAL_PDF].id,
            "tex_artifact_id": published[FINAL_TEX].id,
            "pdf_qa_artifact_id": published[FINAL_PDF_QA].id,
            "language_profile": profile,
            "download_name": metadata["download_name"],
            "revision": stage.revision,
        }

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        draft_payload = self.drafts.get(principal, project_id)
        final_text, final_artifact = self._read_text(
            principal, project_id, FINAL_DRAFT
        )
        conclusion, conclusion_artifact = self._read_text(
            principal, project_id, FINAL_CONCLUSION
        )
        conclusion_report, conclusion_report_artifact = self._read_json(
            principal, project_id, FINAL_CONCLUSION_REPORT
        )
        overview = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        overview_text, overview_text_artifact = self._read_json(
            principal, project_id, FINAL_OVERVIEW_TEXT
        )
        validation, validation_artifact = self._read_json(
            principal, project_id, FINAL_VALIDATION
        )
        release, release_artifact = self._read_json(
            principal, project_id, FINAL_RELEASE
        )
        docx = self._artifact(principal, project_id, FINAL_DOCX)
        docx_qa, docx_qa_artifact = self._read_json(
            principal, project_id, FINAL_DOCX_QA
        )
        manuscript_state, manuscript_state_artifact = self._read_json(
            principal, project_id, FINAL_MANUSCRIPT_STATE
        )
        render_manifest, render_manifest_artifact = self._read_json(
            principal, project_id, FINAL_RENDER_MANIFEST
        )
        pdf_qa, pdf_qa_artifact = self._read_json(
            principal, project_id, FINAL_PDF_QA
        )
        tex_artifact = self._artifact(principal, project_id, FINAL_TEX)
        pdf_artifact = self._artifact(principal, project_id, FINAL_PDF)
        compile_log_artifact = self._artifact(
            principal, project_id, FINAL_PDF_COMPILE_LOG
        )
        state = self.repository.get_stage_state(principal.user_id, project_id, "final")
        current_draft_id = str(draft_payload.get("draft_artifact_id") or "")
        approved = bool(draft_payload.get("draft_approval_current"))
        conclusion_current = bool(
            conclusion_artifact
            and conclusion_report_artifact
            and approved
            and conclusion_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
            and conclusion_report_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
        )
        overview_current = bool(
            overview
            and overview_text_artifact
            and approved
            and overview.metadata.get("source_draft_artifact_id") == current_draft_id
            and overview_text_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
        )
        final_current = bool(
            final_artifact
            and final_artifact.metadata.get("source_draft_artifact_id") == current_draft_id
            and approved
            and final_artifact.metadata.get("conclusion_artifact_id")
            == (conclusion_artifact.id if conclusion_artifact else "")
            and final_artifact.metadata.get("overview_artifact_id")
            == (overview.id if overview else "")
            and final_artifact.metadata.get("overview_text_artifact_id")
            == (overview_text_artifact.id if overview_text_artifact else "")
            and (not conclusion_artifact or conclusion_current)
            and (not overview and not overview_text_artifact or overview_current)
        )
        release_current = bool(
            final_current
            and validation_artifact
            and validation.get("valid")
            and release_artifact
            and release.get("status") == "released"
            and release.get("source_draft_artifact_id") == current_draft_id
            and validation_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
            and release_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
        )
        docx_current = bool(
            docx
            and final_artifact
            and final_current
            and release_current
            and docx.metadata.get("source_final_artifact_id") == final_artifact.id
            and release_artifact
            and docx.metadata.get("source_release_artifact_id")
            == release_artifact.id
            and (not docx_qa_artifact or docx_qa.get("status") == "pass")
        )
        pdf_bundle = (
            manuscript_state_artifact,
            render_manifest_artifact,
            pdf_qa_artifact,
            tex_artifact,
            pdf_artifact,
            compile_log_artifact,
        )
        pdf_current = bool(
            all(pdf_bundle)
            and final_artifact
            and final_current
            and release_current
            and all(
                artifact.metadata.get("source_final_artifact_id") == final_artifact.id
                and artifact.metadata.get("source_release_artifact_id")
                == release_artifact.id
                for artifact in pdf_bundle
                if artifact is not None
            )
            and manuscript_state.get("validation", {}).get("valid")
            and render_manifest.get("template") == "modern-survey"
            and render_manifest.get("language_profile") in SUPPORTED_PROFILES
            and render_manifest.get("shell_escape") is False
            and pdf_qa.get("status") in {"pass", "pass_with_warnings"}
            and not pdf_qa.get("blocking_issues")
            and pdf_qa.get("all_fonts_embedded") is True
        )
        overview_url = f"/api/v1/artifacts/{overview.id}/content" if overview else ""
        docx_url = f"/api/v1/artifacts/{docx.id}/content" if docx else ""
        pdf_url = (
            f"/api/v1/artifacts/{pdf_artifact.id}/content" if pdf_artifact else ""
        )
        tex_url = (
            f"/api/v1/artifacts/{tex_artifact.id}/content" if tex_artifact else ""
        )
        final_jobs = [
            job
            for job in self.repository.list_project_jobs(
                principal.user_id, project_id, limit=50
            )
            if job.job_type in {
                "final.conclusion",
                "final.overview",
                "final.build",
                "final.export",
                "final.pdf",
            }
        ]
        latest_final_job = final_jobs[0] if final_jobs else None
        active_final_job = next(
            (
                job
                for job in final_jobs
                if job.status in {"queued", "running", "cancel_requested"}
            ),
            None,
        )
        validation_report = ""
        if validation:
            validation_report = "\n".join(
                (
                    "## Final audit",
                    "",
                    f"- Status: {'passed' if validation.get('valid') else 'blocked'}",
                    f"- Artifact references: {len(validation.get('referenced_artifact_ids') or [])}",
                    f"- Citation callouts: {', '.join(map(str, validation.get('citation_callouts') or [])) or 'none'}",
                    f"- Listed references: {', '.join(map(str, validation.get('listed_references') or [])) or 'none'}",
                    f"- Sources: {', '.join(validation.get('source_paper_ids') or []) or 'none'}",
                    f"- Blocking issues: {', '.join(validation.get('blocking_issues') or []) or 'none'}",
                    f"- Warnings: {', '.join(validation.get('warning_issues') or []) or 'none'}",
                    "",
                )
            )
        release_report = ""
        if release:
            release_report = "\n".join(
                (
                    "## Release report",
                    "",
                    f"- Status: {release.get('status', 'unknown')}",
                    f"- Draft artifact: {release.get('source_draft_artifact_id', '')}",
                    f"- Source papers: {', '.join(release.get('source_paper_ids') or []) or 'none'}",
                    f"- Warnings: {', '.join(release.get('validation_warning_issues') or []) or 'none'}",
                    f"- Released at: {release.get('released_at', '')}",
                    "",
                )
            )
        return {
            "project_id": project_id,
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "draft_approval_current": approved,
            "draft_approval": {
                **dict(draft_payload.get("draft_approval") or {}),
                "record": dict(draft_payload.get("draft_approval") or {}),
            },
            "final_draft_md": final_text,
            "final_artifact_id": final_artifact.id if final_artifact else "",
            "final_current": final_current,
            "conclusion_generated_md": conclusion,
            "conclusion_artifact_id": conclusion_artifact.id if conclusion_artifact else "",
            "conclusion_report": conclusion_report,
            "conclusion_report_artifact_id": (
                conclusion_report_artifact.id if conclusion_report_artifact else ""
            ),
            "conclusion_current": conclusion_current,
            "overview_figure_url": overview_url,
            "overview_figure_path": overview_url,
            "overview_figure_exists": bool(overview),
            "overview_figure_current": overview_current,
            "overview_artifact_id": overview.id if overview else "",
            "overview_text": overview_text,
            "overview_text_artifact_id": (
                overview_text_artifact.id if overview_text_artifact else ""
            ),
            "validation": validation,
            "validation_artifact_id": validation_artifact.id if validation_artifact else "",
            "release": release,
            "release_artifact_id": release_artifact.id if release_artifact else "",
            "release_current": release_current,
            "docx_artifact_id": docx.id if docx else "",
            "docx_url": docx_url,
            "final_draft_docx_path": docx_url,
            "final_draft_docx_exists": docx_current,
            "final_draft_docx_stale": bool(docx and not docx_current),
            "docx_qa": docx_qa,
            "docx_qa_artifact_id": docx_qa_artifact.id if docx_qa_artifact else "",
            "manuscript_state": manuscript_state,
            "render_manifest": render_manifest,
            "pdf_qa": pdf_qa,
            "pdf_artifact_id": pdf_artifact.id if pdf_artifact else "",
            "pdf_url": pdf_url,
            "tex_artifact_id": tex_artifact.id if tex_artifact else "",
            "tex_url": tex_url,
            "pdf_language_profile": str(
                render_manifest.get("language_profile") or ""
            ),
            "final_pdf_exists": pdf_current,
            "final_pdf_stale": bool(pdf_artifact and not pdf_current),
            "active_final_job_id": (
                active_final_job.id if active_final_job else ""
            ),
            "active_final_job_type": (
                active_final_job.job_type if active_final_job else ""
            ),
            "latest_final_job_id": (
                latest_final_job.id if latest_final_job else ""
            ),
            "latest_final_job_type": (
                latest_final_job.job_type if latest_final_job else ""
            ),
            "latest_final_job_status": (
                latest_final_job.status if latest_final_job else ""
            ),
            "final_audit_report_md": validation_report,
            "release_report_md": release_report,
            "freshness": {
                "draft_stale": not approved,
                "final_stale": bool(final_artifact and not final_current),
                "release_stale": bool(release_artifact and not release_current),
                "pdf_stale": bool(pdf_artifact and not pdf_current),
                "stale": not approved
                or bool(final_artifact and not final_current)
                or bool(release_artifact and not release_current)
                or bool(pdf_artifact and not pdf_current),
            },
        }
