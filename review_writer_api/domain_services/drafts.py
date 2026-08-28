"""PostgreSQL-native draft assembly, editing, quality, rewrite, and approval."""

from __future__ import annotations

import json
import hashlib
import re
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
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
from review_writer_core.draft_bibliography import (
    citation_entries_from_draft,
    strip_numeric_callouts,
)
from review_writer_core.paragraph_markers import ensure_prose_paragraph_markers
from review_writer_core.publication_caption import (
    figure_rights_fields,
    normalize_publication_caption,
)
from review_writer_core.publication_voice import publication_voice_issues
from review_writer_core.writing_contracts import (
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
)


DRAFT_DOCUMENT = "draft/manuscript.md"
DRAFT_QUALITY = "draft/quality.json"
DRAFT_REWRITES = "draft/rewrite-candidates.json"
DRAFT_OPTIMIZATIONS = "draft/optimization-proposals.json"
DRAFT_OVERLAYS = "draft/rewrite-overlays.json"
DRAFT_APPROVAL = "draft/approval.json"
SECTION_INDEX = "sections/section_drafts.json"
SECTION_EVIDENCE = "sections/evidence_package.json"
SECTION_WRITING_PLAN = "sections/writing_plan.json"
FIGURE_MANIFEST = "figures/manifest.json"
PARAGRAPH_MARKER = re.compile(
    r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->"
)
_REFERENCE_WEB_RESIDUE = re.compile(
    r"\b(?:Cite\s+This|Read\s+Online|Article\s+Recommendations?|Supporting\s+Information)\b.*$",
    re.I,
)


def _clean_reference_field(value: Any) -> str:
    text = " ".join(str(value or "").replace("\u00ad", "").split()).strip()
    text = _REFERENCE_WEB_RESIDUE.sub("", text).strip(" .;,|")
    text = re.sub(r"\s*[★☆*]+\s*", " ", text)
    return " ".join(text.split()).strip(" .;,|")


def _clean_reference_doi(value: Any) -> str:
    text = _clean_reference_field(value)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    return text.rstrip(".,;)")


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
        metadata_builder: Callable[
            [str, dict[str, ArtifactRecord]], dict[str, Any]
        ]
        | None = None,
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
                metadata=(
                    metadata_builder(logical_name, published)
                    if metadata_builder is not None
                    else dict(metadata or {})
                ),
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
        evidence_package: dict[str, Any] | None = None,
    ) -> str:
        evidence_registry = {
            str(row.get("evidence_key") or ""): row
            for row in (evidence_package or {}).get("evidence_registry") or []
            if isinstance(row, dict) and str(row.get("evidence_key") or "")
        }
        evidence_by_paper_chunk = {
            (
                str(row.get("paper_id") or ""),
                str(row.get("chunk_id") or ""),
            ): row
            for row in evidence_registry.values()
            if str(row.get("paper_id") or "") and str(row.get("chunk_id") or "")
        }
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
        # Assign final reference numbers by first appearance in the manuscript.
        # Matrix order includes uncited papers and therefore cannot be reused as
        # publication numbering without creating gaps.
        citation_numbers: dict[str, int] = {}
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
                cited_papers: list[str] = []
                claim_bound_parts: list[str] = []
                for realization in paragraph.get("claim_realizations") or []:
                    if not isinstance(realization, dict):
                        continue
                    sentence = strip_numeric_callouts(
                        str(realization.get("text") or "")
                    )
                    if not sentence:
                        continue
                    citation_group = list(
                        dict.fromkeys(
                            str(value).strip()
                            for value in realization.get("citation_group") or []
                            if str(value or "").strip()
                        )
                    )
                    callouts: list[int] = []
                    for paper_id in citation_group:
                        if paper_id not in citation_numbers:
                            citation_numbers[paper_id] = len(citation_numbers) + 1
                        callouts.append(citation_numbers[paper_id])
                        cited_paper_ids.add(paper_id)
                        if paper_id not in cited_papers:
                            cited_papers.append(paper_id)
                    if callouts:
                        claim_bound_parts.append(
                            f"{sentence} [{', '.join(str(value) for value in callouts)}]"
                        )
                    else:
                        claim_bound_parts.append(sentence)

                if claim_bound_parts:
                    # Section prose already contains Matrix-order numbers.  The
                    # Claim realization and its Paper IDs are the source of
                    # truth; rebuilding here prevents two numbering systems
                    # from surviving into the manuscript.
                    text = " ".join(claim_bound_parts)
                else:
                    cited_papers = list(
                        dict.fromkeys(
                            str(value).strip()
                            for value in (
                                paragraph.get("cited_paper_ids")
                                or (
                                    [paragraph.get("paper_id")]
                                    if paragraph.get("paper_id")
                                    else []
                                )
                            )
                            if str(value or "").strip()
                        )
                    )
                    callouts = []
                    for paper_id in cited_papers:
                        if paper_id not in citation_numbers:
                            citation_numbers[paper_id] = len(citation_numbers) + 1
                        callouts.append(citation_numbers[paper_id])
                        cited_paper_ids.add(paper_id)
                    text = strip_numeric_callouts(text)
                    if callouts:
                        text = f"{text} [{', '.join(str(value) for value in callouts)}]"
                paragraph_evidence_rows: list[dict[str, Any]] = []
                claim_ids: list[str] = []
                for realization in paragraph.get("claim_realizations") or []:
                    if not isinstance(realization, dict):
                        continue
                    claim_id = str(realization.get("claim_id") or "")
                    if claim_id:
                        claim_ids.append(claim_id)
                    for ref in realization.get("evidence_refs") or []:
                        if not isinstance(ref, dict):
                            continue
                        key = str(ref.get("evidence_key") or "")
                        registered = evidence_registry.get(key, {})
                        paragraph_evidence_rows.append(
                            {
                                "evidence_id": str(
                                    ref.get("evidence_id")
                                    or registered.get("evidence_id")
                                    or ""
                                ),
                                "evidence_key": key,
                                "paper_id": str(registered.get("paper_id") or ""),
                            }
                        )
                for claim_evidence in paragraph.get("evidence") or []:
                    if not isinstance(claim_evidence, dict):
                        continue
                    paper_id = str(claim_evidence.get("paper_id") or "")
                    claim_id = str(claim_evidence.get("claim_id") or "")
                    if claim_id:
                        claim_ids.append(claim_id)
                    for chunk_id in claim_evidence.get("chunk_ids") or []:
                        registered = evidence_by_paper_chunk.get(
                            (paper_id, str(chunk_id)), {}
                        )
                        paragraph_evidence_rows.append(
                            {
                                "evidence_id": str(registered.get("evidence_id") or ""),
                                "evidence_key": str(registered.get("evidence_key") or ""),
                                "paper_id": paper_id,
                            }
                        )
                paragraph_evidence_ids = list(
                    dict.fromkeys(
                        str(row.get("evidence_id") or "")
                        for row in paragraph_evidence_rows
                        if str(row.get("evidence_id") or "")
                    )
                )
                figure_blocks: list[str] = []
                figure_callouts: list[str] = []
                for figure in figures_by_paragraph.get(paragraph_id, []):
                    figure_number += 1
                    output_id = str(figure["output_artifact_id"])
                    paper_id = str(figure.get("paper_id") or "").strip()
                    caption_text = str(figure.get("source_caption_text") or "").strip()
                    role = str(figure.get("representative_role") or "unknown")
                    normalized_caption = normalize_publication_caption(
                        caption_text,
                        representative_role=role,
                        source_label=figure.get("source_label"),
                        context_title=figure.get("section_heading"),
                    )
                    caption_body = str(
                        figure.get("publication_caption_text")
                        or normalized_caption.publication_text
                        or ""
                    ).strip()
                    caption_plain = str(
                        figure.get("alt_text")
                        or normalized_caption.alt_text
                        or f"Figure {figure_number}"
                    ).strip()
                    rights = figure_rights_fields(figure)
                    source_reference_number = citation_numbers.get(paper_id)
                    render_mode = str(
                        figure.get("render_mode")
                        or figure.get("mode")
                        or figure.get("status")
                        or ""
                    ).casefold()
                    source_reuse = rights.get("source_relationship") == "source_attributed"
                    if source_reuse and source_reference_number:
                        credit_verb = (
                            "Reproduced"
                            if any(
                                marker in render_mode
                                for marker in ("original", "retained", "source")
                            )
                            else "Adapted"
                        )
                        credit = f"{credit_verb} from Ref. {source_reference_number}."
                        caption_body = " ".join(
                            value
                            for value in (
                                f"{caption_body.rstrip('.')}." if caption_body else "",
                                credit,
                            )
                            if value
                        )
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
                    figure_evidence_ids = list(
                        dict.fromkeys(
                            str(row.get("evidence_id") or "")
                            for row in paragraph_evidence_rows
                            if str(row.get("evidence_id") or "")
                            and (
                                not paper_id
                                or str(row.get("paper_id") or "") == paper_id
                            )
                        )
                    )
                    role_text = {
                        "workflow": "shows the study workflow discussed here",
                        "core_transformation": "summarizes the core transformation discussed here",
                        "mechanism": "depicts the proposed mechanistic framework discussed here",
                        "mechanism_model": "depicts the proposed mechanistic framework discussed here",
                        "scope": "summarizes the reported scope or result pattern discussed here",
                        "scope_samples": "summarizes the reported scope or sample pattern discussed here",
                        "quantitative_results": "summarizes the quantitative result discussed here",
                        "comparison_ablation": "supports the comparison discussed here",
                        "paper_overview": "summarizes the study's overall research strategy",
                        "conceptual_overview": "provides a conceptual overview for this discussion",
                        "structure_image": "shows the representative structure or image discussed here",
                        "unknown": "provides source-linked visual context for this discussion",
                    }.get(role, "provides source-linked visual context for this discussion")
                    figure_callouts.append(f"Figure {figure_number} {role_text}.")
                    metadata = json.dumps(
                        {
                            "figure_id": figure.get("figure_id"),
                            "paper_id": paper_id,
                            "target_paragraph_id": paragraph_id,
                            "output_artifact_id": output_id,
                            "representative_role": role,
                            "published_label": f"Figure {figure_number}",
                            "interpretation_basis": interpretation_basis,
                            "claim_ids": list(dict.fromkeys(claim_ids)),
                            "evidence_ids": paragraph_evidence_ids,
                            "figure_evidence_ids": figure_evidence_ids,
                            "caption_normalization_status": normalized_caption.status,
                            "caption_normalization_version": normalized_caption.version,
                            "caption_quality": figure.get("caption_quality")
                            or normalized_caption.manifest_fields().get("caption_quality"),
                            "source_reference_number": source_reference_number,
                            "rights_status": rights.get("rights_status"),
                            "source_relationship": rights.get("source_relationship"),
                            "permission_status": rights.get("permission_status"),
                            "permission_record": rights.get("permission_record"),
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
                authors = (
                    ", ".join(
                        _clean_reference_field(item)
                        for item in raw_authors
                        if _clean_reference_field(item)
                    )
                    if isinstance(raw_authors, list)
                    else _clean_reference_field(raw_authors)
                )
                reference_parts = [
                    authors,
                    _clean_reference_field(value("title")),
                    _clean_reference_field(value("journal")),
                    _clean_reference_field(
                        value("bibliographic_year") or value("year")
                    ),
                    _clean_reference_doi(value("doi")),
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
        evidence_package, evidence_package_artifact = self._read_json(
            principal, project_id, SECTION_EVIDENCE, required=False
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
            evidence_package,
        )
        markdown, overlay_replay = self._apply_rewrite_overlays(markdown, overlays)
        expected_current_artifacts = {
            SECTION_INDEX: sections_artifact.id,
            FIGURE_MANIFEST: manifest_artifact.id,
            MATRIX_LOGICAL_NAME: matrix_artifact.id,
        }
        if evidence_package_artifact is not None:
            expected_current_artifacts[SECTION_EVIDENCE] = evidence_package_artifact.id
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
                    "source_section_evidence_artifact_id": (
                        evidence_package_artifact.id
                        if evidence_package_artifact is not None
                        else ""
                    ),
                    "source_rewrite_overlay_artifact_id": (
                        overlay_artifact.id if overlay_artifact else ""
                    ),
                    "overlay_replay": overlay_replay,
                    "operation": "assemble",
                },
                expected_current_artifacts=expected_current_artifacts,
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
        evidence = self._artifact(principal, project_id, SECTION_EVIDENCE)
        metadata = dict(draft.metadata if draft else {})
        source_evidence_id = str(
            metadata.get("source_section_evidence_artifact_id") or ""
        )
        upstream_stale = bool(
            draft
            and (
                not sections
                or not figures
                or not matrix
                or metadata.get("source_sections_artifact_id") != sections.id
                or metadata.get("source_figure_manifest_artifact_id") != figures.id
                or metadata.get("source_matrix_artifact_id") != matrix.id
                or (
                    source_evidence_id
                    and (
                        evidence is None
                        or source_evidence_id != evidence.id
                    )
                )
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
        matrix, matrix_artifact = self._read_json(
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
            "draft_manual_paragraph_ids": sorted(
                str(value)
                for value in (
                    (draft_artifact.metadata if draft_artifact else {}).get(
                        "unverified_manual_paragraph_ids"
                    )
                    or []
                )
                if str(value).strip()
            ),
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
        if operation == "full-edit" or operation.startswith("paragraph-edit:"):
            canonical, _marker_report = ensure_prose_paragraph_markers(canonical)
            canonical = canonical.rstrip() + "\n"
        if canonical == current_text:
            raise WorkflowValidationError("The edited draft has no content change.")
        metadata = dict(current.metadata)
        metadata["operation"] = operation
        metadata["previous_draft_artifact_id"] = current.id
        if operation == "full-edit" or operation.startswith("paragraph-edit:"):
            before = {
                str(row["paragraph_id"]): self._normalized(str(row["text"]))
                for row in self._paragraph_spans(current_text)
            }
            after = {
                str(row["paragraph_id"]): self._normalized(str(row["text"]))
                for row in self._paragraph_spans(canonical)
            }
            changed = {
                paragraph_id
                for paragraph_id, paragraph_text in after.items()
                if before.get(paragraph_id) != paragraph_text
            }
            if operation.startswith("paragraph-edit:"):
                changed.add(operation.split(":", 1)[1])
            metadata["unverified_manual_paragraph_ids"] = sorted(
                {
                    str(value)
                    for value in metadata.get("unverified_manual_paragraph_ids") or []
                    if str(value).strip()
                }
                | changed
            )
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
        target_evidence_id = str(
            artifact.metadata.get("source_section_evidence_artifact_id") or ""
        )
        target_overlay_id = str(
            artifact.metadata.get("source_rewrite_overlay_artifact_id") or ""
        )
        current_evidence = self._artifact(principal, project_id, SECTION_EVIDENCE)
        current_overlay = self._artifact(principal, project_id, DRAFT_OVERLAYS)
        bundle_restore_needed = bool(
            target_evidence_id
            and (
                current_evidence is None or current_evidence.id != target_evidence_id
            )
        ) or bool(
            target_overlay_id
            and (current_overlay is None or current_overlay.id != target_overlay_id)
        )
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
        if bundle_restore_needed:
            files: dict[
                str,
                tuple[bytes | Callable[[dict[str, ArtifactRecord]], bytes], str],
            ] = {}
            expected_currents = {DRAFT_DOCUMENT: current.id}
            if target_evidence_id:
                target_evidence = self.artifacts.resolve_owned_artifact(
                    principal.user_id, target_evidence_id
                )
                if (
                    target_evidence.artifact.project_id != project_id
                    or target_evidence.artifact.logical_name != SECTION_EVIDENCE
                ):
                    raise WorkflowNotFound("Draft evidence version not found.")
                files[SECTION_EVIDENCE] = (
                    target_evidence.path.read_bytes(),
                    target_evidence.artifact.artifact_type,
                )
                if current_evidence is not None:
                    expected_currents[SECTION_EVIDENCE] = current_evidence.id
            if target_overlay_id:
                target_overlay = self.artifacts.resolve_owned_artifact(
                    principal.user_id, target_overlay_id
                )
                if (
                    target_overlay.artifact.project_id != project_id
                    or target_overlay.artifact.logical_name != DRAFT_OVERLAYS
                ):
                    raise WorkflowNotFound("Draft overlay version not found.")
                files[DRAFT_OVERLAYS] = (
                    target_overlay.path.read_bytes(),
                    target_overlay.artifact.artifact_type,
                )
                if current_overlay is not None:
                    expected_currents[DRAFT_OVERLAYS] = current_overlay.id
            files[DRAFT_DOCUMENT] = (
                resolved.path.read_bytes(),
                artifact.artifact_type,
            )
            restore_metadata = {
                **dict(artifact.metadata),
                "operation": "restore-optimization-bundle",
                "restored_artifact_id": artifact.id,
                "replaced_artifact_id": current.id,
            }

            def restore_artifact_metadata(
                logical_name: str,
                published_so_far: dict[str, ArtifactRecord],
            ) -> dict[str, Any]:
                value = dict(restore_metadata)
                if logical_name == DRAFT_DOCUMENT:
                    if SECTION_EVIDENCE in published_so_far:
                        value["source_section_evidence_artifact_id"] = (
                            published_so_far[SECTION_EVIDENCE].id
                        )
                    if DRAFT_OVERLAYS in published_so_far:
                        value["source_rewrite_overlay_artifact_id"] = (
                            published_so_far[DRAFT_OVERLAYS].id
                        )
                return value

            with self._write_lock:
                published, state = self._publish_files(
                    principal,
                    project_id,
                    files,
                    expected_revision=revision,
                    metadata=restore_metadata,
                    metadata_builder=restore_artifact_metadata,
                    approval_events=[event],
                    expected_current_artifacts=expected_currents,
                )
            return {
                "draft_artifact_id": published[DRAFT_DOCUMENT].id,
                "restored_from_draft_artifact_id": artifact.id,
                "section_evidence_artifact_id": (
                    published[SECTION_EVIDENCE].id
                    if SECTION_EVIDENCE in published
                    else target_evidence_id
                ),
                "rewrite_overlay_artifact_id": (
                    published[DRAFT_OVERLAYS].id
                    if DRAFT_OVERLAYS in published
                    else target_overlay_id
                ),
                "bundle_restored": True,
                "revision": state.revision,
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
        max_iterations: int = 2,
        min_case_words: int = CASE_PARAGRAPH_MIN_WORDS,
        max_case_words: int = CASE_PARAGRAPH_MAX_WORDS,
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
        compatibility = self.compatibility_payload(principal, project_id)
        return {
            **compatibility,
            "project_id": project_id,
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "expected_revision": payload["revision"],
            "draft_text": payload["first_draft_md"],
            "paragraphs": payload["paragraphs"],
            "goal": max(0.0, min(float(goal), 100.0)),
            "paragraph_goal": max(0.0, min(float(paragraph_goal), 100.0)),
            "max_iterations": max(1, min(int(max_iterations), 10)),
            "min_case_words": safe_min_words,
            "max_case_words": safe_max_words,
            "citation_identity": citation_entries_from_draft(
                payload["first_draft_md"],
                dict(compatibility.get("section_index") or {}),
            ),
        }

    def compatibility_payload(
        self, principal: Principal, project_id: str
    ) -> dict[str, Any]:
        project = self.repository.get_owned_project(principal.user_id, project_id)
        matrix, matrix_artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME, required=False
        )
        sections, sections_artifact = self._read_json(
            principal, project_id, SECTION_INDEX, required=False
        )
        section_evidence, section_evidence_artifact = self._read_json(
            principal, project_id, SECTION_EVIDENCE, required=False
        )
        figures, _figures_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        blueprint, _blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        writing_plan, writing_plan_artifact = self._read_json(
            principal, project_id, SECTION_WRITING_PLAN, required=False
        )
        overlays, overlay_artifact = self._read_json(
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
            "section_evidence": section_evidence,
            "writing_plan": writing_plan,
            "figure_manifest": figures,
            "figure_artifact_paths": artifact_paths,
            "library_metadata": library_metadata,
            "rewrite_overlays": overlays,
            "taxonomy_profile": str(
                project.taxonomy_profile if project is not None else "general_academic"
            ),
            "source_matrix_artifact_id": matrix_artifact.id if matrix_artifact else "",
            "source_sections_artifact_id": sections_artifact.id if sections_artifact else "",
            "source_section_evidence_artifact_id": (
                section_evidence_artifact.id if section_evidence_artifact else ""
            ),
            "source_writing_plan_artifact_id": (
                writing_plan_artifact.id if writing_plan_artifact else ""
            ),
            "source_rewrite_overlay_artifact_id": (
                overlay_artifact.id if overlay_artifact else ""
            ),
        }

    def automatic_synthesis_source(
        self,
        principal: Principal,
        project_id: str,
        *,
        text: str | None = None,
        draft: ArtifactRecord | None = None,
    ) -> dict[str, Any]:
        """Return only source-verified Draft prose for automatic downstream synthesis."""

        if text is None or draft is None:
            text, draft = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        excluded = {
            str(value)
            for value in quality.get("unverified_manual_paragraph_ids") or []
            if str(value).strip()
        }
        if (
            quality_artifact is None
            or quality.get("source_draft_artifact_id") != draft.id
        ):
            excluded = set()
        filtered = str(text)
        removed: list[str] = []
        for paragraph in reversed(self._paragraph_spans(filtered)):
            paragraph_id = str(paragraph["paragraph_id"])
            if paragraph_id not in excluded:
                continue
            filtered = (
                filtered[: int(paragraph["start"])]
                + "\n"
                + filtered[int(paragraph["marker_end"]) :]
            )
            removed.append(paragraph_id)
        return {
            "draft_text": filtered.rstrip() + "\n",
            "source_draft_artifact_id": draft.id,
            "source_quality_artifact_id": quality_artifact.id if quality_artifact else "",
            "excluded_manual_paragraph_ids": sorted(removed),
            "warning_required": bool(removed),
        }

    @staticmethod
    def _paragraph_contracts(job_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        contracts: dict[str, dict[str, Any]] = {}
        for section in (job_payload.get("section_index") or {}).get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            for paragraph in section.get("paragraphs") or []:
                if not isinstance(paragraph, dict) or not paragraph.get("paragraph_id"):
                    continue
                claim_ids = [
                    str(item.get("claim_id") or "")
                    for item in paragraph.get("claim_realizations") or []
                    if isinstance(item, dict) and str(item.get("claim_id") or "")
                ]
                claim_ids.extend(
                    str(item.get("claim_id") or "")
                    for item in paragraph.get("evidence") or []
                    if isinstance(item, dict) and str(item.get("claim_id") or "")
                )
                question_ids = [
                    str(item.get("question_id") or item.get("field_id") or "")
                    for item in paragraph.get("claim_realizations") or []
                    if isinstance(item, dict)
                    and str(item.get("question_id") or item.get("field_id") or "")
                ]
                contracts[str(paragraph["paragraph_id"])] = {
                    "section_id": section_id,
                    "claim_ids": list(dict.fromkeys(claim_ids)),
                    "question_ids": list(dict.fromkeys(question_ids)),
                    "allowed_papers": list(
                        dict.fromkeys(
                            str(value)
                            for value in (
                                paragraph.get("cited_paper_ids")
                                or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                            )
                            if str(value or "").strip()
                        )
                    ),
                }
        for section in (job_payload.get("writing_plan") or {}).get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            for claim in section.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                paragraph_id = str(claim.get("paragraph_id") or "")
                claim_id = str(claim.get("claim_id") or "")
                if not paragraph_id or not claim_id:
                    continue
                contract = contracts.setdefault(
                    paragraph_id,
                    {
                        "section_id": section_id,
                        "claim_ids": [],
                        "question_ids": [],
                        "allowed_papers": [],
                    },
                )
                if claim_id not in contract["claim_ids"]:
                    contract["claim_ids"].append(claim_id)
                question_id = str(
                    claim.get("question_id") or claim.get("field_id") or ""
                )
                if question_id and question_id not in contract["question_ids"]:
                    contract["question_ids"].append(question_id)
        return contracts

    @classmethod
    def _repair_evidence_package(
        cls,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Persist original-source passages selected during Draft evaluation.

        The feedback loop already performs a targeted full-text recheck inside
        the paragraph's structured Paper-ID boundary.  This method turns those
        selected passages into a versioned Evidence Package repair instead of
        discarding them after paragraph rewriting.
        """

        package = deepcopy(job_payload.get("section_evidence") or {})
        if not isinstance(package, dict):
            package = {}
        registry = package.get("evidence_registry")
        registry = list(registry) if isinstance(registry, list) else []
        package["evidence_registry"] = registry
        sections = package.get("sections")
        sections = list(sections) if isinstance(sections, list) else []
        package["sections"] = sections
        section_by_id = {
            str(section.get("section_id") or ""): section
            for section in sections
            if isinstance(section, dict)
        }
        contracts = cls._paragraph_contracts(job_payload)
        matrix_papers = {
            str(row.get("paper_id") or "")
            for row in (job_payload.get("matrix") or {}).get("rows") or []
            if isinstance(row, dict) and str(row.get("paper_id") or "")
        }
        scores = {
            str(row.get("paragraph_id") or ""): row
            for row in built.get("paragraph_scores") or []
            if isinstance(row, dict) and str(row.get("paragraph_id") or "")
        }
        source_entries = {
            str(row.get("paragraph_id") or ""): row
            for row in (built.get("source_check") or {}).get("entries") or []
            if isinstance(row, dict) and str(row.get("paragraph_id") or "")
        }
        existing_keys = {
            str(row.get("evidence_key") or "")
            for row in registry
            if isinstance(row, dict) and str(row.get("evidence_key") or "")
        }
        dispositions: dict[str, Any] = {}
        added: list[dict[str, Any]] = []
        affected_sections: set[str] = set()
        affected_paragraphs: set[str] = set()
        for paragraph_id, entry in source_entries.items():
            contract = contracts.get(paragraph_id, {})
            section_id = str(contract.get("section_id") or "")
            allowed = set(contract.get("allowed_papers") or []) & matrix_papers
            score = scores.get(paragraph_id, {})
            selected_refs = {
                str(value)
                for value in score.get("source_evidence_refs")
                or entry.get("source_evidence_refs")
                or []
                if str(value).strip()
            }
            claim_ids = list(contract.get("claim_ids") or [])
            question_ids = list(contract.get("question_ids") or [])
            unsupported = [
                str(value).strip()
                for value in score.get("unsupported_claims")
                or entry.get("unsupported_claims")
                or []
                if str(value).strip()
            ]
            if unsupported:
                for unsupported_text in unsupported:
                    if len(claim_ids) == 1 and len(unsupported) == 1:
                        disposition_id = claim_ids[0]
                    else:
                        unsupported_digest = hashlib.sha256(
                            unsupported_text.encode("utf-8")
                        ).hexdigest()[:16]
                        disposition_id = (
                            f"paragraph:{paragraph_id}:unsupported:{unsupported_digest}"
                        )
                    dispositions[disposition_id] = {
                        "claim_id": disposition_id,
                        "candidate_claim_ids": claim_ids,
                        "paragraph_id": paragraph_id,
                        "section_id": section_id,
                        "disposition": "downgraded_due_to_insufficient_evidence",
                        "unsupported_claim": unsupported_text,
                        "source_check_status": str(
                            score.get("source_check_status") or "not_assessed"
                        ),
                    }
                affected_paragraphs.add(paragraph_id)
                if section_id:
                    affected_sections.add(section_id)
            if not section_id or not selected_refs:
                continue
            section = section_by_id.get(section_id)
            if section is None:
                continue
            hits = section.get("hits")
            hits = list(hits) if isinstance(hits, list) else []
            section["hits"] = hits
            section_keys = {
                str(row.get("evidence_key") or "")
                for row in hits
                if isinstance(row, dict) and str(row.get("evidence_key") or "")
            }
            for paper in entry.get("papers") or []:
                if not isinstance(paper, dict):
                    continue
                paper_id = str(paper.get("paper_id") or "")
                if paper_id not in allowed:
                    continue
                for passage in paper.get("passages") or []:
                    if not isinstance(passage, dict):
                        continue
                    source_ref = str(passage.get("ref") or "")
                    text = " ".join(str(passage.get("text") or "").split()).strip()
                    if source_ref not in selected_refs or not text:
                        continue
                    digest = hashlib.sha256(
                        f"{paper_id}|{source_ref}|{text}".encode("utf-8")
                    ).hexdigest()
                    evidence_key = f"sha256:{digest}"
                    row = {
                        "evidence_id": f"EV-{digest[:12].upper()}",
                        "evidence_key": evidence_key,
                        "paper_id": paper_id,
                        "chunk_id": source_ref,
                        "source_block_id": source_ref,
                        "page": passage.get("page"),
                        "page_start": passage.get("page"),
                        "page_end": passage.get("page"),
                        "text": text,
                        "content": text,
                        "source_ref": source_ref,
                        "content_type": "body",
                        "claim_eligible": True,
                        "counts_as_evidence": True,
                        "match_type": "direct_match",
                        "match_reason": "draft_targeted_original_source_recheck",
                        "source_channel": "draft_targeted_source_recheck",
                        "support_level": "direct",
                        "claim_ids": claim_ids,
                        "question_ids": question_ids,
                        "retrieval_passes": ["draft_targeted_source_recheck"],
                        "is_neighbor": False,
                        "repair_source_paragraph_id": paragraph_id,
                    }
                    row_added = False
                    if evidence_key not in existing_keys:
                        registry.append(row)
                        existing_keys.add(evidence_key)
                        row_added = True
                    if evidence_key not in section_keys:
                        hits.append(dict(row))
                        section_keys.add(evidence_key)
                        row_added = True
                    if row_added:
                        added.append(
                            {
                                "paragraph_id": paragraph_id,
                                "section_id": section_id,
                                "paper_id": paper_id,
                                "evidence_key": evidence_key,
                                "source_ref": source_ref,
                            }
                        )
                    affected_paragraphs.add(paragraph_id)
                    affected_sections.add(section_id)
            section["hit_count"] = len(hits)
            section["claim_eligible_hit_count"] = sum(
                bool(row.get("claim_eligible"))
                for row in hits
                if isinstance(row, dict)
            )

        # Preserve an explicit outcome for unsupported statements that the
        # candidate successfully narrowed or removed.  Without this trace the
        # safer prose would look like a silent Claim deletion.
        for change in built.get("review_changes") or []:
            if not isinstance(change, dict):
                continue
            paragraph_id = str(change.get("paragraph_id") or "")
            contract = contracts.get(paragraph_id, {})
            section_id = str(contract.get("section_id") or "")
            claim_ids = list(contract.get("claim_ids") or [])
            before = {
                str(value).strip()
                for value in change.get("unsupported_claims_before") or []
                if str(value).strip()
            }
            after = {
                str(value).strip()
                for value in change.get("unsupported_claims_after") or []
                if str(value).strip()
            }
            for unsupported_text in sorted(before - after):
                digest = hashlib.sha256(
                    unsupported_text.encode("utf-8")
                ).hexdigest()[:16]
                disposition_id = (
                    claim_ids[0]
                    if len(claim_ids) == 1 and len(before) == 1
                    else f"paragraph:{paragraph_id}:narrowed:{digest}"
                )
                dispositions[disposition_id] = {
                    "claim_id": disposition_id,
                    "candidate_claim_ids": claim_ids,
                    "paragraph_id": paragraph_id,
                    "section_id": section_id,
                    "disposition": "downgraded_due_to_insufficient_evidence",
                    "outcome": "narrowed",
                    "original_unsupported_claim": unsupported_text,
                    "source_check_status_before": str(
                        change.get("source_check_status_before") or ""
                    ),
                    "source_check_status_after": str(
                        change.get("source_check_status_after") or ""
                    ),
                }
                affected_paragraphs.add(paragraph_id)
                if section_id:
                    affected_sections.add(section_id)

        # Recompute section summaries from the repaired direct hits so the UI
        # does not keep reporting a gap that this optimization already fixed.
        for section in sections:
            if not isinstance(section, dict):
                continue
            hits = [
                row for row in section.get("hits") or []
                if isinstance(row, dict)
            ]
            direct_papers = {
                str(row.get("paper_id") or "")
                for row in hits
                if bool(row.get("claim_eligible"))
                and str(row.get("paper_id") or "")
            }
            primary_states = [
                dict(row)
                for row in section.get("primary_paper_states") or []
                if isinstance(row, dict)
            ]
            for state in primary_states:
                if str(state.get("paper_id") or "") in direct_papers:
                    state["status"] = "writeable"
                    state["diagnostic"] = "none"
                    state["draft_repair"] = True
            primary_ids = [
                str(row.get("paper_id") or "")
                for row in primary_states
                if str(row.get("paper_id") or "")
            ]
            primary_id_set = set(primary_ids)
            writeable = [
                str(row.get("paper_id") or "")
                for row in primary_states
                if str(row.get("status") or "") == "writeable"
            ]
            context_only = [
                str(row.get("paper_id") or "")
                for row in primary_states
                if str(row.get("status") or "") == "context_only"
            ]
            unresolved = [
                str(row.get("paper_id") or "")
                for row in primary_states
                if str(row.get("status") or "") == "unresolved"
            ]
            if primary_ids:
                section_status = (
                    "ready"
                    if len(writeable) == len(primary_ids)
                    else "partial"
                    if writeable or context_only
                    else "insufficient_evidence"
                )
            else:
                section_status = (
                    "ready" if direct_papers else str(section.get("status") or "")
                )
            for plan in section.get("query_plans") or []:
                if not isinstance(plan, dict):
                    continue
                question_id = str(plan.get("question_id") or "")
                evidence_matched = {
                    str(row.get("paper_id") or "")
                    for row in hits
                    if bool(row.get("claim_eligible"))
                    and question_id
                    and question_id in {
                        str(value) for value in row.get("question_ids") or []
                    }
                    and str(row.get("paper_id") or "")
                }
                repair_matched = {
                    str(row.get("paper_id") or "")
                    for row in hits
                    if bool(row.get("claim_eligible"))
                    and question_id
                    and question_id
                    in {str(value) for value in row.get("question_ids") or []}
                    and "draft_targeted_source_recheck"
                    in {str(value) for value in row.get("retrieval_passes") or []}
                    and str(row.get("paper_id") or "")
                }
                matched = sorted(evidence_matched)
                matched_primary = [
                    paper_id for paper_id in matched if paper_id in primary_id_set
                ]
                coverage_policy = str(
                    plan.get("coverage_policy")
                    or (
                        "all_primary"
                        if question_id == "section_focus"
                        else "any_primary"
                        if question_id.startswith("required_claim_")
                        else "evidence_bearing"
                    )
                )
                plan["matched_papers"] = matched
                plan["matched_primary_papers"] = matched_primary
                plan["expected_primary_papers"] = (
                    list(primary_ids)
                    if coverage_policy == "all_primary"
                    else list(matched_primary)
                )
                if repair_matched:
                    plan["draft_repair_matched_papers"] = sorted(repair_matched)
                if coverage_policy == "all_primary":
                    plan["status"] = (
                        "sufficient"
                        if (
                            primary_ids
                            and len(matched_primary) == len(primary_ids)
                        )
                        or (not primary_ids and bool(matched))
                        else "partial"
                        if matched
                        else "insufficient"
                    )
                elif matched:
                    plan["status"] = "sufficient"
                elif coverage_policy == "any_primary":
                    plan["status"] = "insufficient"
                else:
                    plan["status"] = "not_reported"
                diagnostics = dict(plan.get("diagnostics_by_primary_paper") or {})
                for paper_id in primary_ids:
                    if paper_id in matched_primary:
                        diagnostics[paper_id] = "none"
                    elif coverage_policy == "evidence_bearing":
                        diagnostics[paper_id] = "not_required"
                plan["diagnostics_by_primary_paper"] = diagnostics
            unresolved_required_questions = {
                str(plan.get("question_id") or "")
                for plan in section.get("query_plans") or []
                if isinstance(plan, dict)
                and bool(
                    plan.get("required_for_section")
                    or str(plan.get("question_id") or "") == "section_focus"
                    or str(plan.get("question_id") or "").startswith(
                        "required_claim_"
                    )
                )
                and str(plan.get("status") or "") == "insufficient"
            }
            section.update(
                {
                    "hits": hits,
                    "hit_count": len(hits),
                    "claim_eligible_hit_count": sum(
                        bool(row.get("claim_eligible")) for row in hits
                    ),
                    "paper_count": len(direct_papers),
                    "retrieval_mode": (
                        "lexical+draft_targeted_source_recheck"
                        if direct_papers
                        else str(section.get("retrieval_mode") or "")
                    ),
                    "status": section_status,
                    "primary_paper_states": primary_states,
                    "covered_primary_paper_count": len(writeable),
                    "writeable_primary_papers": writeable,
                    "context_only_primary_papers": context_only,
                    "unresolved_primary_papers": unresolved,
                    "missing_primary_papers": unresolved,
                    "corpus_gap_questions": [
                        question_id
                        for question_id in sorted(unresolved_required_questions)
                        if question_id
                    ],
                }
            )

        repaired_at = utc_now().isoformat()
        summary = {
            "status": "completed",
            "repaired_at": repaired_at,
            "added_evidence_count": len(added),
            "downgraded_claim_count": len(dispositions),
            "affected_section_ids": sorted(affected_sections),
            "affected_paragraph_ids": sorted(affected_paragraphs),
            "added_evidence": added,
        }
        history = list(package.get("draft_repair_history") or [])
        history.append(summary)
        package.update(
            {
                "schema_version": max(2, int(package.get("schema_version") or 1)),
                "source_evidence_package_artifact_id": str(
                    job_payload.get("source_section_evidence_artifact_id") or ""
                ),
                "repaired_at": repaired_at,
                "draft_repair_history": history[-20:],
            }
        )
        return package, summary, dispositions

    @staticmethod
    def _quality_routing(
        built: dict[str, Any], job_payload: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Route quality failures to the earliest workflow stage that can fix them."""

        paragraph_sections = {
            str(paragraph.get("paragraph_id") or ""): str(
                section.get("section_id") or ""
            )
            for section in (job_payload.get("section_index") or {}).get("sections") or []
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict) and paragraph.get("paragraph_id")
        }
        evidence_sections = {
            str(section.get("section_id") or ""): section
            for section in (job_payload.get("section_evidence") or {}).get("sections") or []
            if isinstance(section, dict)
        }
        source_checks = {
            str(row.get("paragraph_id") or ""): row
            for row in (built.get("source_check") or {}).get("entries") or []
            if isinstance(row, dict) and str(row.get("paragraph_id") or "")
        }
        paragraph_scores = {
            str(row.get("paragraph_id") or ""): row
            for row in built.get("paragraph_scores") or []
            if isinstance(row, dict)
        }
        stage_priority = {"discovery": 0, "planning": 1, "sections": 2, "draft": 3}
        labels = {
            "discovery": "Return to literature retrieval",
            "planning": "Return to Matrix and outline",
            "sections": "Repair affected section evidence inside Draft optimization",
            "draft": "Revise wording in the current Draft",
        }
        discovery_terms = {
            "search", "retrieval", "coverage", "corpus", "literature",
            "missing_primary", "recall", "sampling", "publication_bias",
        }
        planning_terms = {
            "taxonomy", "classification", "matrix", "outline", "organization",
            "section_structure", "category", "comparison_axis",
        }
        section_terms = {
            "source", "evidence", "citation", "factual", "fact", "mechanism",
            "result", "quantitative", "support", "claim", "reference",
            "scope",
        }

        routed_issues: list[dict[str, Any]] = []
        by_stage: dict[str, list[str]] = {stage: [] for stage in stage_priority}
        for index, raw_issue in enumerate(built.get("issues") or [], 1):
            if not isinstance(raw_issue, dict):
                continue
            issue = dict(raw_issue)
            paragraph_id = str(issue.get("paragraph_id") or "")
            score = paragraph_scores.get(paragraph_id, {})
            section_id = paragraph_sections.get(paragraph_id, "")
            section_evidence = evidence_sections.get(section_id, {})
            source_status = str(
                score.get("source_check_status")
                or issue.get("source_check_status")
                or "not_assessed"
            ).casefold()
            route = str(score.get("route") or issue.get("route") or "").casefold()
            failed_dimensions = [
                str(value) for value in score.get("failed_dimensions")
                or issue.get("failed_dimensions") or []
                if str(value).strip()
            ]
            searchable = " ".join(
                [
                    route,
                    source_status,
                    *failed_dimensions,
                    str(issue.get("diagnosis") or issue.get("message") or ""),
                ]
            ).casefold()
            source_entry = source_checks.get(paragraph_id, {})
            has_original_passages = any(
                paper.get("passages")
                for paper in source_entry.get("papers") or []
                if isinstance(paper, dict)
            )
            issue_type = "draft_wording"
            repair_route = "paragraph_rewrite"
            auto_repairable = True
            internal_repair_stage = "draft"
            reference_map_problem = any(
                marker in searchable
                for marker in (
                    "citation_reference_map_mismatch",
                    "citation-map mismatch",
                    "citation map mismatch",
                    "unlisted bibliography",
                    "callout",
                )
            )
            has_term = lambda terms: any(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
                    searchable,
                )
                for term in terms
            )
            if has_term(discovery_terms):
                stage = "discovery"
                action = "Broaden or correct the retrieval scope, then refresh Matrix evidence."
                issue_type = "literature_coverage_gap"
                repair_route = "manual_online_retrieval_decision"
                auto_repairable = False
                internal_repair_stage = "discovery"
            elif has_term(planning_terms):
                stage = "planning"
                action = "Correct the Matrix classification or section structure before rewriting."
                issue_type = "planning_structure"
                repair_route = "planning_revision"
                auto_repairable = False
                internal_repair_stage = "planning"
            elif reference_map_problem and source_status in {
                "verified",
                "not_applicable",
                "not_assessed",
            }:
                stage = "draft"
                action = "Rebuild the citation and reference map automatically after paragraph rewriting."
                issue_type = "citation_reference_mapping"
                repair_route = "deterministic_reference_rebuild"
                internal_repair_stage = "draft"
            elif (
                source_status
                in {"partially_supported", "unsupported", "needs_human_review"}
                or route == "local_source_recheck"
                or has_term(section_terms)
            ):
                # Evidence repair is executed by the one-click Draft optimizer;
                # the user does not need to navigate back to Sections.
                stage = "draft"
                internal_repair_stage = "sections"
                issue_type = "claim_evidence_gap"
                repair_route = (
                    "targeted_evidence_then_paragraph_rewrite"
                    if has_original_passages
                    else "claim_downgrade_then_paragraph_rewrite"
                )
                auto_repairable = source_status != "needs_human_review"
                action = (
                    "Automatically attach the matching local-source passages and rewrite only this paragraph."
                    if has_original_passages
                    else "Keep the Claim trace, lower unsupported detail, and rewrite only this paragraph."
                )
            else:
                stage = "draft"
                action = "Revise this paragraph without changing supported scientific claims."
            question_diagnostics = [
                {
                    "question_id": str(question.get("question_id") or ""),
                    "status": str(question.get("status") or ""),
                    "diagnostics_by_primary_paper": dict(
                        question.get("diagnostics_by_primary_paper") or {}
                    ),
                }
                for question in section_evidence.get("query_plans") or []
                if isinstance(question, dict)
                and str(question.get("status") or "")
                in {"partial", "abstract_limited", "insufficient"}
            ]
            issue_id = str(issue.get("issue_id") or issue.get("id") or f"PAR-{index:03d}")
            issue.update(
                {
                    "issue_id": issue_id,
                    "section_id": section_id,
                    "source_check_status": source_status,
                    "source_evidence_refs": list(score.get("source_evidence_refs") or []),
                    "recommended_return_stage": stage,
                    "recommended_action": action,
                    "internal_repair_stage": internal_repair_stage,
                    "issue_type": issue_type,
                    "repair_route": repair_route,
                    "auto_repairable": auto_repairable,
                    "section_evidence_status": str(section_evidence.get("status") or ""),
                    "unresolved_primary_papers": list(
                        section_evidence.get("unresolved_primary_papers") or []
                    ),
                    "corpus_gap_questions": list(
                        section_evidence.get("corpus_gap_questions") or []
                    ),
                    "question_diagnostics": question_diagnostics,
                }
            )
            routed_issues.append(issue)
            by_stage[stage].append(issue_id)

        active_stages = [stage for stage, issue_ids in by_stage.items() if issue_ids]
        recommended = min(active_stages, key=stage_priority.get) if active_stages else "draft"
        return routed_issues, {
            "recommended_return_stage": recommended,
            "recommended_action": labels[recommended],
            "issues_by_stage": by_stage,
            "counts_by_stage": {stage: len(issue_ids) for stage, issue_ids in by_stage.items()},
        }

    @staticmethod
    def _quality_root_causes(
        issues: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Group repeated paragraph symptoms into stable executable repair roots."""

        grouped: dict[str, dict[str, Any]] = {}
        for issue in issues:
            route = str(issue.get("repair_route") or "paragraph_rewrite")
            section_id = str(issue.get("section_id") or "")
            issue_id = str(issue.get("issue_id") or issue.get("id") or "")
            if route in {
                "deterministic_reference_rebuild",
                "manual_online_retrieval_decision",
            }:
                scope = "global"
            elif route in {
                "targeted_evidence_then_paragraph_rewrite",
                "claim_downgrade_then_paragraph_rewrite",
                "planning_revision",
            }:
                scope = section_id or "unassigned-section"
            else:
                scope = str(issue.get("paragraph_id") or issue_id or "unknown")
            key = f"{route}:{scope}"
            root = grouped.setdefault(
                key,
                {
                    "root_cause_id": "ROOT-"
                    + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper(),
                    "repair_route": route,
                    "scope": scope,
                    "issue_type": str(issue.get("issue_type") or "draft_wording"),
                    "auto_repairable": bool(issue.get("auto_repairable", True)),
                    "requires_user_decision": not bool(
                        issue.get("auto_repairable", True)
                    ),
                    "issue_ids": [],
                    "paragraph_ids": [],
                    "section_ids": [],
                    "paper_ids": [],
                    "status": "open",
                    "repair_attempts": [],
                },
            )
            root["auto_repairable"] = bool(root["auto_repairable"]) and bool(
                issue.get("auto_repairable", True)
            )
            root["requires_user_decision"] = not root["auto_repairable"]
            for target, values in (
                ("issue_ids", [issue_id]),
                ("paragraph_ids", [str(issue.get("paragraph_id") or "")]),
                ("section_ids", [section_id]),
                (
                    "paper_ids",
                    [
                        *[str(value) for value in issue.get("unresolved_primary_papers") or []],
                        *[
                            str(value)
                            for value in issue.get("paper_ids") or []
                        ],
                    ],
                ),
            ):
                root[target] = list(
                    dict.fromkeys(
                        [*root[target], *[value for value in values if value]]
                    )
                )
            issue["root_cause_id"] = root["root_cause_id"]
        roots = list(grouped.values())
        roots.sort(key=lambda row: str(row.get("root_cause_id") or ""))
        tasks = [
            {
                "task_id": f"TASK-{root['root_cause_id'][5:]}",
                "root_cause_id": root["root_cause_id"],
                "repair_route": root["repair_route"],
                "target": {
                    "paragraph_ids": root["paragraph_ids"],
                    "section_ids": root["section_ids"],
                    "paper_ids": root["paper_ids"],
                },
                "status": (
                    "requires_user_input"
                    if root["requires_user_decision"]
                    else "queued"
                ),
                "auto_repairable": root["auto_repairable"],
            }
            for root in roots
        ]
        return roots, tasks

    @staticmethod
    def _repair_summary(
        source_quality: dict[str, Any],
        current_roots: list[dict[str, Any]],
        *,
        evidence_repair: dict[str, Any] | None = None,
        reference_repair: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_ids = {
            str(row.get("root_cause_id") or "")
            for row in source_quality.get("root_causes") or []
            if isinstance(row, dict) and str(row.get("root_cause_id") or "")
        }
        remaining_ids = {
            str(row.get("root_cause_id") or "")
            for row in current_roots
            if str(row.get("root_cause_id") or "")
        }
        resolved_ids = sorted(source_ids - remaining_ids)
        remaining_user = [
            str(row.get("root_cause_id") or "")
            for row in current_roots
            if row.get("requires_user_decision")
        ]
        automatic_remaining = [
            str(row.get("root_cause_id") or "")
            for row in current_roots
            if row.get("auto_repairable")
        ]
        changed = bool(
            resolved_ids
            or (evidence_repair or {}).get("added_evidence_count")
            or (reference_repair or {}).get("changed")
        )
        if not source_ids and current_roots:
            repair_status = "not_started"
        elif not current_roots:
            repair_status = "completed"
        elif remaining_user and not automatic_remaining:
            repair_status = "requires_user_input"
        elif changed:
            repair_status = "partial_success"
        else:
            repair_status = "requires_user_input" if remaining_user else "partial_success"
        return {
            "repair_status": repair_status,
            "source_root_cause_ids": sorted(source_ids),
            "resolved_root_cause_ids": resolved_ids,
            "remaining_root_cause_ids": sorted(remaining_ids),
            "requires_user_input_root_cause_ids": remaining_user,
            "updated_at": utc_now().isoformat(),
        }

    @classmethod
    def _manual_claim_review(
        cls, current: ArtifactRecord, built: dict[str, Any]
    ) -> dict[str, Any]:
        manual_ids = {
            str(value)
            for value in current.metadata.get("unverified_manual_paragraph_ids") or []
            if str(value).strip()
        }
        scores = {
            str(row.get("paragraph_id") or ""): row
            for row in built.get("paragraph_scores") or []
            if isinstance(row, dict)
        }
        entries = []
        verified: list[str] = []
        unverified: list[str] = []
        for paragraph_id in sorted(manual_ids):
            score = scores.get(paragraph_id, {})
            source_status = str(score.get("source_check_status") or "not_assessed")
            evidence_refs = [
                str(value)
                for value in score.get("source_evidence_refs") or []
                if str(value).strip()
            ]
            is_verified = source_status == "verified" and bool(evidence_refs)
            (verified if is_verified else unverified).append(paragraph_id)
            entries.append(
                {
                    "paragraph_id": paragraph_id,
                    "status": "verified" if is_verified else "unverified",
                    "source_check_status": source_status,
                    "source_evidence_refs": evidence_refs,
                    "export_allowed": True,
                    "automatic_synthesis_allowed": is_verified,
                }
            )
        return {
            "entries": entries,
            "verified_manual_paragraph_ids": verified,
            "unverified_manual_paragraph_ids": unverified,
            "warning_required": bool(unverified),
        }

    @staticmethod
    def _quality_status_partition(quality: dict[str, Any]) -> dict[str, Any]:
        """Separate repair work from non-overridable release integrity."""

        issues = [
            row for row in quality.get("issues") or [] if isinstance(row, dict)
        ]
        repair_ids = [
            str(row.get("issue_id") or row.get("id") or "")
            for row in issues
            if str(row.get("issue_id") or row.get("id") or "")
        ]
        integrity: list[str] = [
            str(value)
            for value in quality.get("hard_gate_failures") or []
            if str(value).strip()
        ]
        if quality.get("unverified_manual_paragraph_ids"):
            integrity.append("unverified_manual_claims")
        reference_repair = (
            quality.get("reference_repair")
            if isinstance(quality.get("reference_repair"), dict)
            else {}
        )
        if reference_repair and (
            reference_repair.get("status") == "not_applied"
            or reference_repair.get("unresolved_callouts")
            or reference_repair.get("conflicts")
        ):
            integrity.append("citation_identity_unresolved")
        integrity = list(dict.fromkeys(integrity))
        return {
            "repair_required": bool(repair_ids),
            "repair_required_issue_ids": repair_ids,
            "release_integrity_failure": bool(integrity),
            "release_integrity_failures": integrity,
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
        routed_issues, routing = self._quality_routing(built, job_payload)
        root_causes, repair_tasks = self._quality_root_causes(routed_issues)
        manual_review = self._manual_claim_review(current, built)
        quality = {
            **{key: value for key, value in built.items() if key != "source_draft_artifact_id"},
            "issues": routed_issues,
            "routing": routing,
            "root_causes": root_causes,
            "repair_summary": self._repair_summary({}, root_causes),
            "manual_claim_review": manual_review,
            "verified_manual_paragraph_ids": manual_review[
                "verified_manual_paragraph_ids"
            ],
            "unverified_manual_paragraph_ids": manual_review[
                "unverified_manual_paragraph_ids"
            ],
            "source_draft_artifact_id": current.id,
            "score": score,
            "goal": float(built.get("goal") or job_payload.get("goal") or 90),
            "status": "completed",
            "evaluated_at": utc_now().isoformat(),
        }
        quality.update(self._quality_status_partition(quality))
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
            "repair_tasks": repair_tasks,
            "repair_status": str(
                (quality.get("repair_summary") or {}).get("repair_status")
                or "not_started"
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
        current_quality, current_quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        source_quality_id = str(job_payload.get("source_quality_artifact_id") or "")
        if source_quality_id and (
            current_quality_artifact is None
            or current_quality_artifact.id != source_quality_id
        ):
            raise WorkflowConflict(
                "Draft evaluation changed while batch optimization was running."
            )
        repaired_evidence, evidence_repair, claim_dispositions = (
            self._repair_evidence_package(job_payload, built)
        )
        deterministic_base_text = str(
            built.get("deterministic_base_draft_text") or current_text
        ).rstrip() + "\n"
        reference_repair = (
            dict(built.get("reference_repair") or {})
            if isinstance(built.get("reference_repair"), dict)
            else {"status": "not_requested", "changed": False}
        )
        review_changes = [
            dict(item)
            for item in built.get("review_changes") or []
            if isinstance(item, dict)
        ]
        review_candidate_text = str(
            built.get("review_candidate_draft_text") or ""
        )
        if review_changes and not bool(
            built.get("review_candidate_full_draft_evaluated")
        ):
            raise WorkflowConflict(
                "The combined optimization candidate was not evaluated as a full draft."
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
                "deterministic_base_draft_text",
                "reference_repair",
            }
        }
        routing_payload = {**job_payload, "section_evidence": repaired_evidence}
        routed_issues, routing = self._quality_routing(built, routing_payload)
        root_causes, repair_tasks = self._quality_root_causes(routed_issues)
        summary_source_quality = dict(current_quality or {})
        if not summary_source_quality.get("root_causes"):
            legacy_issues = [
                dict(row)
                for row in summary_source_quality.get("issues") or []
                if isinstance(row, dict)
            ]
            legacy_roots, _legacy_tasks = self._quality_root_causes(legacy_issues)
            summary_source_quality["root_causes"] = legacy_roots
        repair_summary = self._repair_summary(
            summary_source_quality,
            root_causes,
            evidence_repair=evidence_repair,
            reference_repair=reference_repair,
        )
        quality_base.update(
            {
                "issues": routed_issues,
                "routing": routing,
                "root_causes": root_causes,
                "repair_summary": repair_summary,
                "score": score,
                "total_score": score,
                "goal": float(built.get("goal") or job_payload.get("goal") or 90),
                "status": "completed",
                "quality_scope": "full_draft",
                "reference_repair": reference_repair,
                "evidence_repair": evidence_repair,
                "claim_dispositions": claim_dispositions,
                "evaluated_at": utc_now().isoformat(),
            }
        )
        feedback_status = (
            dict(built.get("feedback_status"))
            if isinstance(built.get("feedback_status"), dict)
            else {}
        )
        final_feedback_status = {
            **feedback_status,
            "phase": "completed",
            "full_draft_evaluated": bool(
                built.get("review_candidate_full_draft_evaluated")
                or not review_changes
            ),
        }
        quality_base["feedback_status"] = final_feedback_status
        quality_base.update(self._quality_status_partition(quality_base))

        # Only paragraph bodies may enter a batch proposal.  Rebuilding from
        # the current manuscript prevents a model from silently changing
        # headings, figure markers, references, or document structure outside
        # the reviewable paragraph comparisons.
        candidate_text, changes = self._optimization_candidate(
            deterministic_base_text, model_text
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
        draft_changed = candidate_text != current_text
        evidence_changed = bool(
            evidence_repair.get("added_evidence_count")
        )
        deterministic_repair_changed = bool(reference_repair.get("changed"))

        if not draft_changed and not evidence_changed and not deterministic_repair_changed:
            # There is no text or evidence mutation to approve, but the exact
            # current manuscript was still fully evaluated.  Publish that
            # fresh, specifically routed Quality report instead of leaving an
            # older generic issue queue visible.
            quality = {
                **quality_base,
                "source_draft_artifact_id": current.id,
            }
            manual_review = self._manual_claim_review(current, quality)
            quality.update(
                {
                    "manual_claim_review": manual_review,
                    "verified_manual_paragraph_ids": manual_review[
                        "verified_manual_paragraph_ids"
                    ],
                    "unverified_manual_paragraph_ids": manual_review[
                        "unverified_manual_paragraph_ids"
                    ],
                }
            )
            quality.update(self._quality_status_partition(quality))
            expected_currents = {DRAFT_DOCUMENT: current.id}
            if current_quality_artifact is not None:
                expected_currents[DRAFT_QUALITY] = current_quality_artifact.id
            for logical_name, source_key in (
                (SECTION_EVIDENCE, "source_section_evidence_artifact_id"),
                (SECTION_WRITING_PLAN, "source_writing_plan_artifact_id"),
                (DRAFT_OVERLAYS, "source_rewrite_overlay_artifact_id"),
            ):
                source_id = str(job_payload.get(source_key) or "")
                if source_id:
                    expected_currents[logical_name] = source_id
            with self._write_lock:
                published, state = self._publish_files(
                    principal,
                    project_id,
                    {
                        DRAFT_QUALITY: (
                            (
                                json.dumps(quality, ensure_ascii=False, indent=2)
                                + "\n"
                            ).encode(),
                            "json",
                        )
                    },
                    expected_revision=int(job_payload["expected_revision"]),
                    metadata={
                        "operation": "batch-optimization-full-evaluation",
                        "source_draft_artifact_id": current.id,
                    },
                    expected_current_artifacts=expected_currents,
                    invalidate_final=False,
                )
            return {
                "draft_artifact_id": current.id,
                "quality_artifact_id": published[DRAFT_QUALITY].id,
                "score": score,
                "draft_changed": False,
                "proposal_created": False,
                "rewrite_accepted": int(feedback_status.get("rewrite_accepted") or 0),
                "rewrite_rejected": int(feedback_status.get("rewrite_rejected") or 0),
                "rewrite_deferred": int(feedback_status.get("rewrite_deferred") or 0),
                "feedback_status": final_feedback_status,
                "repair_tasks": repair_tasks,
                "repair_status": str(repair_summary.get("repair_status") or "partial_success"),
                "revision": state.revision,
            }
        source_quality = dict(current_quality or {})
        if not source_quality:
            source_quality = {
                **quality_base,
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
            "source_section_evidence_artifact_id": str(
                job_payload.get("source_section_evidence_artifact_id") or ""
            ),
            "source_writing_plan_artifact_id": str(
                job_payload.get("source_writing_plan_artifact_id") or ""
            ),
            "source_rewrite_overlay_artifact_id": str(
                job_payload.get("source_rewrite_overlay_artifact_id") or ""
            ),
            "candidate_draft_text": candidate_text,
            "deterministic_base_draft_text": deterministic_base_text,
            "reference_repair": reference_repair,
            "candidate_evidence_package": repaired_evidence,
            "evidence_repair": evidence_repair,
            "claim_dispositions": claim_dispositions,
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
            "feedback_status": final_feedback_status,
            "status": "pending",
            "created_at": created_at,
        }
        proposal_payload = {"project_id": project_id, "entries": entries}
        expected_currents = {DRAFT_DOCUMENT: current.id}
        for logical_name, payload_key in (
            (DRAFT_QUALITY, "source_quality_artifact_id"),
            (SECTION_EVIDENCE, "source_section_evidence_artifact_id"),
            (SECTION_WRITING_PLAN, "source_writing_plan_artifact_id"),
            (DRAFT_OVERLAYS, "source_rewrite_overlay_artifact_id"),
        ):
            artifact_id = str(entries[proposal_id].get(payload_key) or "")
            if artifact_id:
                expected_currents[logical_name] = artifact_id
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
                    "feedback_status": final_feedback_status,
                    "evidence_repair": evidence_repair,
                    "reference_repair": reference_repair,
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
            "evidence_repair": evidence_repair,
            "reference_repair": reference_repair,
            "claim_dispositions": claim_dispositions,
            "rewrite_accepted": int(feedback_status.get("rewrite_accepted") or 0),
            "rewrite_rejected": int(feedback_status.get("rewrite_rejected") or 0),
            "rewrite_deferred": int(feedback_status.get("rewrite_deferred") or 0),
            "feedback_status": final_feedback_status,
            "repair_tasks": repair_tasks,
            "repair_status": str(repair_summary.get("repair_status") or "partial_success"),
            "revision": state.revision,
        }

    def auto_apply_optimization_proposal(
        self,
        principal: Principal,
        project_id: str,
        proposal_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Apply a whole batch only when every paragraph is demonstrably safe.

        Mixed batches remain pending in the existing comparison UI.  This
        avoids silently dropping an ambiguous paragraph while still making
        the normal, integrity-checked improvement path automatic.
        """

        store, _store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS
        )
        proposal = dict((store.get("entries") or {}).get(proposal_id) or {})
        if not proposal or proposal.get("status") != "pending":
            return {
                "auto_applied": False,
                "auto_apply_status": "proposal_not_pending",
            }
        _current_text, current = self._read_text(
            principal, project_id, DRAFT_DOCUMENT
        )
        manual_ids = {
            str(item)
            for item in current.metadata.get("unverified_manual_paragraph_ids") or []
            if str(item).strip()
        }
        changes = [
            dict(item)
            for item in proposal.get("changes") or []
            if isinstance(item, dict) and item.get("paragraph_id")
        ]
        downgrade_paragraph_ids = {
            str(item.get("paragraph_id") or "")
            for item in (proposal.get("claim_dispositions") or {}).values()
            if isinstance(item, dict) and str(item.get("paragraph_id") or "")
        }
        unsafe: list[dict[str, Any]] = []
        candidate_quality = dict(proposal.get("candidate_quality") or {})
        if candidate_quality.get("hard_gate_failures"):
            unsafe.append(
                {
                    "paragraph_id": "",
                    "reasons": ["full_draft_integrity_failure"],
                }
            )
        for change in changes:
            paragraph_id = str(change.get("paragraph_id") or "")
            evaluation = dict(change.get("candidate_evaluation") or {})
            paragraph_score = dict(evaluation.get("paragraph_score") or {})
            preflight = dict(evaluation.get("local_preflight") or {})
            reasons: list[str] = []
            if paragraph_id in manual_ids:
                reasons.append("user_modified_paragraph")
            if bool(change.get("requires_manual_confirmation")) or bool(
                evaluation.get("requires_manual_confirmation")
            ):
                reasons.append("scientific_ambiguity_requires_confirmation")
            if evaluation.get("evaluation_scope") != "single_paragraph":
                reasons.append("paragraph_re_evaluation_missing")
            if evaluation.get("local_hard_gate_failures"):
                reasons.append("local_integrity_failure")
            if preflight.get("hard_regressions"):
                reasons.append("local_preflight_regression")
            if str(paragraph_score.get("route") or "") == "human_confirmation":
                reasons.append("human_confirmation_route")
            if paragraph_id in downgrade_paragraph_ids and not bool(
                change.get("accuracy_improved")
            ):
                reasons.append("claim_downgrade_did_not_improve_evidence_accuracy")
            try:
                source_score = float(change.get("source_paragraph_score"))
                candidate_score = float(change.get("candidate_paragraph_score"))
            except (TypeError, ValueError):
                if not bool(change.get("accuracy_improved")):
                    reasons.append("score_delta_unavailable")
            else:
                if (
                    candidate_score <= source_score
                    and not bool(change.get("accuracy_improved"))
                ):
                    reasons.append("paragraph_score_not_improved")
            if reasons:
                unsafe.append({"paragraph_id": paragraph_id, "reasons": reasons})

        reference_repair = dict(proposal.get("reference_repair") or {})
        evidence_repair = dict(proposal.get("evidence_repair") or {})
        has_deterministic_repairs = bool(
            reference_repair.get("changed")
            or evidence_repair.get("added_evidence_count")
        )
        changed_paragraph_ids = {
            str(change.get("paragraph_id") or "") for change in changes
        }
        missing_downgrade_rewrites = sorted(
            downgrade_paragraph_ids - changed_paragraph_ids
        )
        if missing_downgrade_rewrites:
            unsafe.extend(
                {
                    "paragraph_id": paragraph_id,
                    "reasons": ["unsupported_claim_was_not_downgraded_in_text"],
                }
                for paragraph_id in missing_downgrade_rewrites
            )
        if (not changes and not has_deterministic_repairs) or unsafe:
            return {
                "auto_applied": False,
                "auto_apply_status": "manual_review_required",
                "manual_review_reasons": unsafe,
            }
        accepted = self.decide_optimization_proposal(
            principal,
            project_id,
            proposal_id,
            decision="accept",
            revision=revision,
            selected_paragraph_ids=[
                str(change.get("paragraph_id") or "") for change in changes
            ],
        )
        return {
            **accepted,
            "auto_applied": True,
            "auto_apply_status": "all_safe_paragraphs_applied",
            "proposal_created": False,
            "draft_changed": bool(accepted.get("draft_changed", True)),
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
        reference_repair = dict(proposal.get("reference_repair") or {})
        evidence_repair = dict(proposal.get("evidence_repair") or {})
        has_automatic_repairs = bool(
            reference_repair.get("changed")
            or evidence_repair.get("added_evidence_count")
        )
        if decision == "accept":
            selected_ids = set(requested_ids or sorted(available_ids))
            unknown = sorted(selected_ids - available_ids)
            if unknown:
                raise WorkflowValidationError(
                    "Unknown optimization paragraph selection: "
                    + ", ".join(unknown)
                )
            if not selected_ids and not has_automatic_repairs:
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
            candidate_evidence = dict(
                proposal.get("candidate_evidence_package") or {}
            )
            if evidence_repair.get("added_evidence_count") and candidate_evidence:
                files[SECTION_EVIDENCE] = (
                    (
                        json.dumps(
                            candidate_evidence, ensure_ascii=False, indent=2
                        )
                        + "\n"
                    ).encode(),
                    "json",
                )
            deterministic_base = str(
                proposal.get("deterministic_base_draft_text") or current_text
            )
            candidate_text = self._optimization_candidate_from_changes(
                deterministic_base, selected_changes
            )
            if not candidate_text.strip() or (
                candidate_text == current_text and not has_automatic_repairs
            ):
                raise WorkflowConflict("Optimization proposal contains no applicable change.")
            files[DRAFT_DOCUMENT] = (
                (candidate_text.rstrip() + "\n").encode("utf-8"),
                "markdown",
            )

            # Every reviewable optimization change is evaluated at paragraph
            # scope before it is shown to the user.  Apply those exact scores
            # whether the user accepts some or all changes.  Previously the
            # all-selected path replaced them with the loop's full-draft
            # snapshot; that snapshot can describe an earlier/best iteration
            # and made the saved score disagree with the comparison UI.
            candidate_quality, scored_changes = (
                self._optimization_quality_from_scored_changes(
                    proposal, selected_changes
                )
            )
            if scored_changes == len(selected_changes) and selected_changes:
                quality_scope = "batch_selected_paragraphs"
            elif selected_ids != available_ids:
                raise WorkflowConflict(
                    "This legacy batch proposal cannot publish a partial selection "
                    "because it has no paragraph-level candidate scores."
                )
            else:
                # Compatibility for old proposals and deterministic-only
                # citation/evidence repairs that predate paragraph scoring.
                candidate_quality = dict(
                    proposal.get("candidate_quality")
                    or proposal.get("source_quality")
                    or {}
                )
                quality_scope = "full_draft"
            candidate_quality.update(
                {
                    "quality_scope": quality_scope,
                    "selected_paragraph_ids": sorted(selected_ids),
                    "reference_repair": reference_repair,
                    "evidence_repair": evidence_repair,
                    "claim_dispositions": dict(
                        proposal.get("claim_dispositions") or {}
                    ),
                    "status": "completed",
                    "evaluated_at": decided_at,
                }
            )
            manual_review = self._manual_claim_review(current, candidate_quality)
            candidate_quality.update(
                {
                    "manual_claim_review": manual_review,
                    "verified_manual_paragraph_ids": manual_review[
                        "verified_manual_paragraph_ids"
                    ],
                    "unverified_manual_paragraph_ids": manual_review[
                        "unverified_manual_paragraph_ids"
                    ],
                }
            )
            candidate_quality.update(
                self._quality_status_partition(candidate_quality)
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
        common_metadata = {
            **dict(current.metadata),
            "operation": f"batch-optimization-{decision}",
            "proposal_id": proposal_id,
            "previous_draft_artifact_id": current.id,
            "reference_repair": reference_repair,
            "evidence_repair": evidence_repair,
        }

        def artifact_metadata(
            logical_name: str, published_so_far: dict[str, ArtifactRecord]
        ) -> dict[str, Any]:
            value = dict(common_metadata)
            if SECTION_EVIDENCE in published_so_far:
                value["source_section_evidence_artifact_id"] = (
                    published_so_far[SECTION_EVIDENCE].id
                )
            if DRAFT_DOCUMENT in published_so_far and logical_name in {
                DRAFT_QUALITY,
                DRAFT_OVERLAYS,
            }:
                value["source_draft_artifact_id"] = (
                    published_so_far[DRAFT_DOCUMENT].id
                )
            return value

        expected_currents = {
            DRAFT_DOCUMENT: current.id,
            DRAFT_OPTIMIZATIONS: store_artifact.id,
        }
        for logical_name, source_key in (
            (DRAFT_QUALITY, "source_quality_artifact_id"),
            (SECTION_EVIDENCE, "source_section_evidence_artifact_id"),
            (SECTION_WRITING_PLAN, "source_writing_plan_artifact_id"),
            (DRAFT_OVERLAYS, "source_rewrite_overlay_artifact_id"),
        ):
            source_id = str(proposal.get(source_key) or "")
            if source_id:
                expected_currents[logical_name] = source_id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=revision,
                metadata=common_metadata,
                metadata_builder=artifact_metadata,
                approval_events=[event],
                expected_current_artifacts=expected_currents,
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
            "section_evidence_artifact_id": (
                published[SECTION_EVIDENCE].id
                if SECTION_EVIDENCE in published
                else str(proposal.get("source_section_evidence_artifact_id") or "")
            ),
            "evidence_repair": evidence_repair,
            "reference_repair": reference_repair,
            "draft_changed": bool(
                decision == "accept" and DRAFT_DOCUMENT in published
            ),
            "score": (
                candidate_quality.get("score")
                if decision == "accept"
                else None
            ),
            "revision": state.revision,
        }

    def _optimization_quality_from_scored_changes(
        self,
        proposal: dict[str, Any],
        selected_changes: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int]:
        candidate_quality = dict(
            proposal.get("source_quality")
            or proposal.get("candidate_quality")
            or {}
        )
        scored_changes = 0
        for change in selected_changes:
            paragraph_id = str(change.get("paragraph_id") or "")
            candidate_evaluation = dict(change.get("candidate_evaluation") or {})
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
        if scored_changes == len(selected_changes) and selected_changes:
            # Avoid accumulating one rounding operation per paragraph.  The
            # comparison UI sums unrounded overall deltas once, so publish the
            # same deterministic total here.
            source_quality = dict(
                proposal.get("source_quality")
                or proposal.get("candidate_quality")
                or {}
            )
            source_score = float(source_quality.get("score") or 0)
            explicit_deltas: list[float] = []
            for change in selected_changes:
                try:
                    explicit_deltas.append(float(change["overall_score_delta"]))
                except (KeyError, TypeError, ValueError):
                    explicit_deltas = []
                    break
            if explicit_deltas:
                exact_score = source_score + sum(explicit_deltas)
            else:
                source_scores = {
                    str(item.get("paragraph_id") or ""): float(
                        item.get("score") or 0
                    )
                    for item in source_quality.get("paragraph_scores") or []
                    if isinstance(item, dict)
                    and str(item.get("paragraph_id") or "")
                }
                paragraph_count = max(1, len(source_scores))
                exact_score = source_score
                for change in selected_changes:
                    paragraph_id = str(change.get("paragraph_id") or "")
                    evaluation = dict(change.get("candidate_evaluation") or {})
                    paragraph_score = dict(evaluation.get("paragraph_score") or {})
                    exact_score += (
                        float(paragraph_score.get("score") or 0)
                        - source_scores.get(paragraph_id, 0.0)
                    ) / paragraph_count
            exact_score = round(max(0.0, min(exact_score, 100.0)), 2)
            candidate_quality["score"] = exact_score
            candidate_quality["total_score"] = exact_score
        return candidate_quality, scored_changes

    def repair_accepted_optimization_quality(
        self,
        principal: Principal,
        project_id: str,
        proposal_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Republish a score that was saved from the old all-selected path.

        This is intentionally a service-level maintenance operation, not a
        public route.  It lets deployments repair an already accepted proposal
        without another paid model evaluation or any manuscript rewrite.
        """

        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS
        )
        proposal = dict((store.get("entries") or {}).get(proposal_id) or {})
        if not proposal or proposal.get("status") != "accepted":
            raise WorkflowConflict("Accepted optimization proposal not found.")
        _current_text, current = self._read_text(
            principal, project_id, DRAFT_DOCUMENT
        )
        if str(current.metadata.get("proposal_id") or "") != proposal_id:
            raise WorkflowConflict(
                "The accepted proposal is not attached to the current Draft."
            )
        _quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY
        )
        selected_ids = {
            str(value)
            for value in proposal.get("selected_paragraph_ids") or []
            if str(value).strip()
        }
        selected_changes = [
            dict(item)
            for item in proposal.get("changes") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") in selected_ids
        ]
        candidate_quality, scored_changes = (
            self._optimization_quality_from_scored_changes(
                proposal, selected_changes
            )
        )
        if not selected_changes or scored_changes != len(selected_changes):
            raise WorkflowConflict(
                "The accepted proposal has no complete paragraph-level scores."
            )
        repaired_at = utc_now().isoformat()
        candidate_quality.update(
            {
                "quality_scope": "batch_selected_paragraphs",
                "selected_paragraph_ids": sorted(selected_ids),
                "reference_repair": dict(proposal.get("reference_repair") or {}),
                "evidence_repair": dict(proposal.get("evidence_repair") or {}),
                "claim_dispositions": dict(
                    proposal.get("claim_dispositions") or {}
                ),
                "status": "completed",
                "evaluated_at": repaired_at,
            }
        )
        manual_review = self._manual_claim_review(current, candidate_quality)
        candidate_quality.update(
            {
                "manual_claim_review": manual_review,
                "verified_manual_paragraph_ids": manual_review[
                    "verified_manual_paragraph_ids"
                ],
                "unverified_manual_paragraph_ids": manual_review[
                    "unverified_manual_paragraph_ids"
                ],
            }
        )
        candidate_quality.update(self._quality_status_partition(candidate_quality))
        candidate_quality["source_draft_artifact_id"] = current.id
        metadata = {
            **dict(current.metadata),
            "operation": "repair-batch-optimization-quality",
            "proposal_id": proposal_id,
            "source_draft_artifact_id": current.id,
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_QUALITY: (
                        (
                            json.dumps(
                                candidate_quality, ensure_ascii=False, indent=2
                            )
                            + "\n"
                        ).encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                metadata=metadata,
                expected_current_artifacts={
                    DRAFT_DOCUMENT: current.id,
                    DRAFT_QUALITY: quality_artifact.id,
                    DRAFT_OPTIMIZATIONS: store_artifact.id,
                },
                invalidate_final=True,
            )
        return {
            "proposal_id": proposal_id,
            "draft_artifact_id": current.id,
            "quality_artifact_id": published[DRAFT_QUALITY].id,
            "score": candidate_quality.get("score"),
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
            min_case_words = int(case_range[0] or CASE_PARAGRAPH_MIN_WORDS)
            max_case_words = int(case_range[1] or CASE_PARAGRAPH_MAX_WORDS)
        elif isinstance(case_range, dict):
            min_case_words = int(
                case_range.get("min_words") or CASE_PARAGRAPH_MIN_WORDS
            )
            max_case_words = int(
                case_range.get("max_words") or CASE_PARAGRAPH_MAX_WORDS
            )
        else:
            min_case_words, max_case_words = (
                CASE_PARAGRAPH_MIN_WORDS,
                CASE_PARAGRAPH_MAX_WORDS,
            )
        compatibility = self.compatibility_payload(principal, project_id)
        return {
            **compatibility,
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
            min_case_words = int(case_range[0] or CASE_PARAGRAPH_MIN_WORDS)
            max_case_words = int(case_range[1] or CASE_PARAGRAPH_MAX_WORDS)
        elif isinstance(case_range, dict):
            min_case_words = int(
                case_range.get("min_words") or CASE_PARAGRAPH_MIN_WORDS
            )
            max_case_words = int(
                case_range.get("max_words") or CASE_PARAGRAPH_MAX_WORDS
            )
        else:
            min_case_words, max_case_words = (
                CASE_PARAGRAPH_MIN_WORDS,
                CASE_PARAGRAPH_MAX_WORDS,
            )
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
        manual_review = self._manual_claim_review(current, updated_quality)
        updated_quality.update(
            {
                "manual_claim_review": manual_review,
                "verified_manual_paragraph_ids": manual_review[
                    "verified_manual_paragraph_ids"
                ],
                "unverified_manual_paragraph_ids": manual_review[
                    "unverified_manual_paragraph_ids"
                ],
            }
        )
        updated_quality.update(self._quality_status_partition(updated_quality))
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
