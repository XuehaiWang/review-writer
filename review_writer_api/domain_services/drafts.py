"""PostgreSQL-native draft assembly, editing, quality, rewrite, and approval."""

from __future__ import annotations

from review_writer_api.paper_labels import library_paper_labels

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from review_writer_core.scientific_facts import (
    attach_repair_fact_context, evidence_repair_has_changes,
    fact_support_spans, fact_identity, merge_facts, fact_is_usable,
)

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import utc_now
from review_writer_api.domain_services.base import ArtifactBackedService
from review_writer_api.domain_services.actions.draft.errors import (
    DraftApprovalBlocked,
    DraftNotReady,
)
from review_writer_api.domain_services.actions.draft.decisions import (
    DraftDecisionActionsMixin,
)
from review_writer_api.domain_services.actions.draft.optimization import (
    DraftOptimizationActionsMixin,
)
from review_writer_api.domain_services.actions.draft.quality import (
    DraftQualityActionsMixin,
)
from review_writer_api.domain_services.actions.draft.rewrite import (
    DraftRewriteActionsMixin,
)
from review_writer_api.domain_services.actions.draft.dialogue import DraftDialogueMixin
from review_writer_core.paragraph_revision import paragraph_keys, dialogue_sections
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.draft_bibliography import (
    citation_map_comment,
    format_citation_group,
    reference_text,
    strip_numeric_callouts,
)
from review_writer_core.chemical_typography import normalize_chemical_typography
from review_writer_core.publication_tables import render_section_comparison
from review_writer_core.draft_issue_routing import (
    quality_issue_paper_ids,
    quality_issue_source_evidence_refs,
    route_draft_issue,
    REPAIR_ROUTING_VERSION,
    planning_adjustments,
    requires_user_decision,
)
from review_writer_core.draft_quality import quality_score
from review_writer_core.stages.draft.text import (
    apply_rewrite_overlays,
    normalize_draft_text,
    optimization_candidate,
    paragraph_spans,
    text_sha256,
)
from review_writer_core.paragraph_markers import ensure_prose_paragraph_markers
from review_writer_core.figure_caption import caption_fields
from review_writer_core.publication_caption import (
    figure_rights_fields,
)
from review_writer_core.publication_voice import publication_voice_issues
from review_writer_core.writing_contracts import (
    DRAFT_PASS_THRESHOLD,
    substantive_quality_findings,
    PARAGRAPH_PASS_THRESHOLD,
    paragraph_finding_is_blocking,
)
from review_writer_core.workflow.artifacts import (
    DRAFT_APPROVAL,
    DRAFT_MANUSCRIPT as DRAFT_DOCUMENT,
    DRAFT_OPTIMIZATION_PROPOSALS as DRAFT_OPTIMIZATIONS,
    DRAFT_QUALITY_REPORT as DRAFT_QUALITY,
    DRAFT_REWRITE_CANDIDATES as DRAFT_REWRITES,
    DRAFT_REWRITE_OVERLAYS as DRAFT_OVERLAYS,
    FIGURE_MANIFEST,
    MATRIX as MATRIX_LOGICAL_NAME,
    SECTION_DRAFTS as SECTION_INDEX,
    SECTION_EVIDENCE_PACKAGE as SECTION_EVIDENCE,
)


class DraftsService(
    DraftDialogueMixin,
    DraftDecisionActionsMixin,
    DraftOptimizationActionsMixin,
    DraftQualityActionsMixin,
    DraftRewriteActionsMixin,
    ArtifactBackedService,
):
    _paragraph_spans = staticmethod(paragraph_spans)
    _normalized = staticmethod(normalize_draft_text)
    _text_sha256 = staticmethod(text_sha256)
    _apply_rewrite_overlays = staticmethod(apply_rewrite_overlays)
    _optimization_candidate = staticmethod(optimization_candidate)

    def __init__(self, repository: WorkflowRepository, artifacts: ArtifactService):
        self.repository = repository
        self.artifacts = artifacts
        self._write_lock = threading.RLock()

    def _revision(self, principal: Principal, project_id: str) -> int:
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        return state.revision if state else 0

    def _validate_repair_lineages(self, principal, evidence_repair):
        expected = evidence_repair.get("fact_agent_source_lineages") or {}
        if not expected:
            return
        from review_writer_api.domain_services.library_index import LibraryIndexService
        index = LibraryIndexService(self.repository.session_factory, self.artifacts.workspace_manager)
        expected_parents = evidence_repair.get("fact_agent_source_parents") or {}
        if expected_parents:
            parents = sorted(set(expected_parents.values()))
            actual_parents = self._supporting_source_parents(self._catalog(principal, parents), parents)
            if actual_parents != expected_parents:
                raise WorkflowConflict("The linked supporting-information scope changed while the repair was pending.")
        for paper_id, lineage in expected.items():
            _paper, _artifacts, _lineage, current = index._paper_and_lineage(principal, paper_id)
            if not lineage or current != lineage:
                raise WorkflowConflict("The article or linked SI changed while the fact repair candidate was pending.")

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
        # Observational results do not revoke approval of unchanged prose.
        if not invalidate_final and DRAFT_DOCUMENT not in files and DRAFT_APPROVAL not in files:
            previous_state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
            if previous_state is not None and previous_state.status == "approved":
                status = "approved"
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
        if DRAFT_DOCUMENT in files and DRAFT_OVERLAYS in files:
            # A historical Draft must point to its matching accepted revision
            # bundle, not the overlay from before acceptance. Publish the
            # overlay before the Draft; Quality can then depend on both.
            ordered = {}
            for name, value in files.items():
                if name == DRAFT_OVERLAYS:
                    continue
                if name == DRAFT_DOCUMENT:
                    ordered[DRAFT_OVERLAYS] = files[DRAFT_OVERLAYS]
                ordered[name] = value
            files = ordered
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
            artifact_metadata = (metadata_builder(logical_name, published)
                                 if metadata_builder is not None else dict(metadata or {}))
            if logical_name == DRAFT_DOCUMENT:
                old_text, old_artifact = self._read_text(principal, project_id, DRAFT_DOCUMENT, required=False)
                old_keys = paragraph_keys(old_text, old_artifact.metadata, old_artifact.id) if old_artifact else {}
                operation = artifact_metadata.get("operation")
                if operation == "assemble":
                    old_keys = {}
                if str(operation or "").startswith("paragraph-edit:") and len(old_keys) != len(self._paragraph_spans(content.decode("utf-8"))):
                    # Splitting a saved paragraph retires its dialogue identity.
                    old_keys.pop(operation.split(":", 1)[1], None)
                if operation == "full-edit":
                    before = {p["paragraph_id"]: p["text"] for p in self._paragraph_spans(old_text)}
                    old_keys = {p["paragraph_id"]: old_keys[p["paragraph_id"]]
                                for p in self._paragraph_spans(content.decode("utf-8"))
                                if p["paragraph_id"] in old_keys and before.get(p["paragraph_id"]) == p["text"]}
                artifact_metadata["paragraph_keys"] = {
                    p["paragraph_id"]: old_keys.get(p["paragraph_id"]) or str(uuid.uuid4())
                    for p in self._paragraph_spans(content.decode("utf-8"))}
            if logical_name in {DRAFT_DOCUMENT, DRAFT_QUALITY} and DRAFT_OVERLAYS in published:
                artifact_metadata["source_rewrite_overlay_artifact_id"] = published[DRAFT_OVERLAYS].id
            published[logical_name] = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                filename,
                logical_name=logical_name,
                artifact_type=artifact_type,
                producer_stage="draft",
                make_current=False,
                metadata=artifact_metadata,
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
        insertion_plan = {
            str(row.get("figure_id") or ""): row
            for row in figure_manifest.get("insertion_plan") or []
            if isinstance(row, dict) and str(row.get("figure_id") or "")
        }
        for row in figure_manifest.get("figures") or []:
            if not isinstance(row, dict) or row.get("status") != "redrawn":
                continue
            decision = insertion_plan.get(str(row.get("figure_id") or ""))
            if insertion_plan and decision is None:
                continue
            if decision is not None and not bool(decision.get("include")):
                continue
            paragraph_id = str(
                (decision or {}).get("target_paragraph_id")
                or row.get("target_paragraph_id")
                or ""
            )
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
        table_number = 0
        rendered_parents: set[str] = set()
        for section in section_index.get("sections") or []:
            if not isinstance(section, dict):
                continue
            if section.get("generation_mode") == "pending_evidence":
                continue
            for depth, parent in enumerate(section.get("parent_headings") or [], start=2):
                if parent["section_id"] not in rendered_parents:
                    parts.append("#" * depth + " " + parent["title"])
                    rendered_parents.add(parent["section_id"])
            heading = str(section.get("heading") or section.get("section_id") or "Section")
            parts.append("#" * int(section.get("heading_level") or 2) + " " + heading)
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
                            f"{sentence} {format_citation_group(callouts)}"
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
                        text = f"{text} {format_citation_group(callouts)}"
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
                    role = str(figure.get("representative_role") or "unknown")
                    display_caption = caption_fields(figure)
                    caption_body = str(display_caption.get("publication_caption_text") or "").strip()
                    caption_plain = caption_body or f"Figure {figure_number}"
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
                    interpretation_basis = display_caption["caption_provenance"]["method"]
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
                    if not re.search(rf"\bFigure\s+{figure_number}\b", text, re.I):
                        figure_callouts.append(f"Figure {figure_number}")
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
                            "caption_normalization_status": display_caption["caption_normalization_status"],
                            "caption_normalization_version": display_caption["caption_normalization_version"],
                            "caption_quality": display_caption["caption_quality"],
                            "caption_provenance": {key: value for key, value in display_caption["caption_provenance"].items()
                                                   if key != "source_text"},
                            "source_reference_number": source_reference_number,
                            "source_identity_status": rights.get(
                                "source_identity_status"
                            ),
                            "source_paper_id": rights.get("source_paper_id"),
                            "source_label": rights.get("source_label"),
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
                    # Place the reference before final punctuation/citation;
                    # do not manufacture a second scientific sentence.
                    suffix = re.search(r"([.!?]?\s*(?:\[[\d,;\s–-]+\]\s*)*)$", text)
                    position = suffix.start() if suffix else len(text)
                    text = f"{text[:position].rstrip()} ({'; '.join(figure_callouts)}){text[position:]}"
                parts.append(f"{text}\n\n<!-- paragraph_id: {paragraph_id} -->")
                parts.extend(figure_blocks)
            comparison = render_section_comparison(
                section, matrix_by_id, citation_numbers, table_number=table_number + 1,
            )
            if comparison:
                table_number += 1
                parts.append(comparison)
        if cited_paper_ids:
            parts.append(citation_map_comment(citation_numbers))
            references = ["## References"]
            for paper_id in sorted(cited_paper_ids, key=citation_numbers.__getitem__):
                number = citation_numbers[paper_id]
                row = matrix_by_id.get(paper_id, {})
                reference = reference_text(row, fallback=f"Paper P{number:03d}")
                references.append(f"[{number}] {reference}")
            parts.append("\n".join(references))
        return normalize_chemical_typography("\n\n".join(part.strip() for part in parts if part.strip()) + "\n")

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
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        metadata = dict(draft.metadata if draft else {})
        source_evidence_id = str(
            metadata.get("source_section_evidence_artifact_id") or ""
        )
        upstream_stale = bool(
            draft
            and (
                (state is not None and state.status == "stale")
                or not sections
                or not figures
                or not matrix
                or metadata.get("source_sections_artifact_id") != sections.id
                or metadata.get("source_figure_manifest_artifact_id") != figures.id
                or not self._matrix_dependency_matches(principal, project_id, metadata.get("source_matrix_artifact_id"), matrix)
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

    @staticmethod
    def _navigation_stage_for_repair(repair_stage: str) -> str:
        """Map a precise repair owner to the existing page navigation stage."""

        return {
            "discovery": "discovery",
            "planning": "planning",
            "library_matrix": "planning",
            "writing_plan": "sections",
            "evidence_package": "draft",
            "figures": "draft",
            "bibliography": "draft",
            "final": "draft",
        }.get(str(repair_stage or "draft"), "draft")

    @classmethod
    def _public_issue_repair_metadata(
        cls, issue: dict[str, Any]
    ) -> dict[str, Any]:
        """Backfill repair routing for immutable quality artifacts from older releases.

        Current evaluations already persist these fields. Historical artifacts must
        remain immutable, so the API decorates only the response returned to the UI.
        """

        if int(issue.get("repair_routing_version") or 0) >= REPAIR_ROUTING_VERSION:
            return dict(issue)
        # Explicit custom actions are not a legacy output of this router.
        if not issue.get("repair_routing_version") and issue.get("repair_action") and not issue.get("repair_route"):
            return dict(issue)
        source_status = str(issue.get("source_check_status") or "not_assessed")
        evaluator_route = str(issue.get("route") or "")
        searchable = " ".join(
            [
                evaluator_route,
                source_status,
                *[
                    str(value)
                    for value in issue.get("failed_dimensions") or []
                ],
                str(issue.get("diagnosis") or issue.get("message") or ""),
            ]
        ).casefold()
        reference_map_problem = any(
            marker in searchable
            for marker in (
                "citation_reference_map_mismatch",
                "citation-map mismatch",
                "citation map mismatch",
                "unlisted bibliography",
            )
        )
        repair = route_draft_issue(
            issue,
            source_status=source_status,
            evaluator_route=evaluator_route,
            has_original_passages=bool(issue.get("source_evidence_refs")),
            reference_map_problem=reference_map_problem,
            source_evidence_refs=list(issue.get("source_evidence_refs") or []),
        )
        repair_stage = str(repair.get("repair_stage") or "draft")
        return {
            **issue,
            "recommended_return_stage": cls._navigation_stage_for_repair(
                repair_stage
            ),
            **repair,
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
        stable_keys = paragraph_keys(text, draft_artifact.metadata, draft_artifact.id) if draft_artifact else {}
        for paragraph in paragraphs:
            paragraph["text_sha256"] = hashlib.sha256(paragraph["text"].encode("utf-8")).hexdigest()
            paragraph["paragraph_key"] = stable_keys[paragraph["paragraph_id"]]
        paragraph_by_id = {row["paragraph_id"]: row for row in paragraphs}
        manifest, _manifest_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        matrix, matrix_artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME, required=False
        )
        paper_display_labels = library_paper_labels(self.repository.session_factory, principal.user_id)
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
        freshness = self._freshness(principal, project_id, draft_artifact)
        quality_current = bool(
            draft_artifact
            and quality_artifact
            and quality.get("source_draft_artifact_id") == draft_artifact.id
            and not freshness["upstream_stale"]
        )
        public_quality = dict(quality) if quality else {}
        issues = []
        for issue in public_quality.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            issue = self._public_issue_repair_metadata(issue)
            issue["blocking"] = paragraph_finding_is_blocking(issue)
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
            public_quality["approval_findings"] = substantive_quality_findings(quality)
            public_quality["blocking_issue_count"] = len(public_quality["approval_findings"])
            public_quality["issues"] = issues
            public_quality["planning_adjustments"] = planning_adjustments(issues)
            public_quality["current"] = quality_current
            if not quality_current:
                public_quality["status"] = "stale"
        approval_current = bool(
            draft_artifact
            and state
            and state.status == "approved"
            and approval.get("status") == "approved"
            and approval.get("draft_artifact_id") == draft_artifact.id
            and not freshness["upstream_stale"]
        )
        versions = self.repository.list_artifacts(
            principal.user_id, project_id, DRAFT_DOCUMENT
        )
        rewrite_candidates = []
        for value in (rewrites.get("entries") or {}).values():
            if not isinstance(value, dict):
                continue
            candidate = dict(value)
            if candidate.get("revision_mode") == "dialogue" and candidate.get("status") == "pending":
                current_target = next((p for p in paragraphs if stable_keys.get(p["paragraph_id"]) == candidate.get("paragraph_key")), None)
                if (not current_target or current_target["text_sha256"] != candidate.get("base_text_sha256")
                        or freshness["upstream_stale"]):
                    candidate["status"] = "stale"
            if (
                candidate.get("status") == "pending"
                and candidate.get("revision_mode") != "dialogue"
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
            source_quality = proposal.get("source_quality")
            if isinstance(source_quality, dict) and source_quality:
                # Repair legacy pending proposals created from rubric payloads,
                # which use ``total_score`` instead of the API ``score`` key.
                proposal["source_score"] = quality_score(source_quality)
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
            proposal.pop("candidate_matrix", None)
            proposal.pop("candidate_evidence_package", None)
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
        dialogue_batch_job = next((job for job in jobs if job.payload.get("revision_mode") == "dialogue_batch" and not job.payload.get("section_id")), None)
        paragraph_task_states = {}
        for job in jobs:
            if job.status not in {"queued", "running", "cancel_requested"}:
                continue
            key = (job.payload.get("dialogue") or {}).get("paragraph_key") or (job.result or {}).get("active_paragraph_key")
            if key:
                paragraph_task_states[key] = {"id": job.id, "status": job.status,
                    "batch": job.payload.get("revision_mode") == "dialogue_batch"}
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
            "first_draft_md": normalize_chemical_typography(text),
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
            "dialogue_batch_job": ({"id": dialogue_batch_job.id, "status": dialogue_batch_job.status,
                "result": dialogue_batch_job.result, "progress_current": dialogue_batch_job.progress_current,
                "progress_total": dialogue_batch_job.progress_total, "error_message": dialogue_batch_job.error_message}
                if dialogue_batch_job else None),
            "sections": dialogue_sections(text, draft_artifact.metadata, draft_artifact.id) if draft_artifact else [],
            "section_task_states": {job.payload["section_id"]: {"id": job.id, "status": job.status}
                for job in jobs if job.payload.get("section_id") and job.status in {"queued", "running", "cancel_requested"}},
            "paragraph_task_states": paragraph_task_states,
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
            "freshness": freshness,
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
        expected_draft_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        if expected_draft_artifact_id and current.id != expected_draft_artifact_id:
            raise WorkflowConflict("Draft changed while merging the paragraph edit.")
        if self._freshness(principal, project_id, current)["upstream_stale"]:
            raise DraftNotReady("Draft inputs changed. Reassemble Draft before editing.")
        canonical = str(text).rstrip() + "\n"
        if operation == "full-edit" or operation.startswith("paragraph-edit:"):
            canonical, _marker_report = ensure_prose_paragraph_markers(canonical)
            canonical = canonical.rstrip() + "\n"
        if canonical == current_text:
            return {"draft_artifact_id": current.id,
                    "revision": self._revision(principal, project_id), "changed": False}
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
        base_text_sha256: str = "",
    ) -> dict[str, Any]:
        # A paragraph token permits unrelated edits, never a stale whole-document
        # overwrite. The existing atomic artifact promotion remains the CAS.
        principal.require(Permission.PROJECT_WRITE)
        for attempt in range(3):
            current_revision = self._revision(principal, project_id)
            markdown, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
            paragraph = next((row for row in self._paragraph_spans(markdown)
                              if row["paragraph_id"] == paragraph_id), None)
            if paragraph is None:
                raise WorkflowNotFound("Draft paragraph not found.")
            if base_text_sha256 and hashlib.sha256(paragraph["text"].encode("utf-8")).hexdigest() != base_text_sha256:
                raise WorkflowConflict("This paragraph changed. Compare the latest saved text before saving.")
            updated = markdown[:paragraph["start"]] + str(text).strip() + markdown[paragraph["end"]:]
            try:
                return self.save_text(
                    principal, project_id, text=updated,
                    revision=current_revision if base_text_sha256 else revision,
                    operation=f"paragraph-edit:{paragraph_id}",
                    expected_draft_artifact_id=current.id,
                )
            except WorkflowConflict:
                if not base_text_sha256 or attempt == 2:
                    raise
        raise WorkflowConflict("Draft is busy. Retry saving this paragraph.")

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
        target_matrix_id = str(
            artifact.metadata.get("source_matrix_artifact_id") or ""
        )
        current_evidence = self._artifact(principal, project_id, SECTION_EVIDENCE)
        current_overlay = self._artifact(principal, project_id, DRAFT_OVERLAYS)
        current_matrix = self._artifact(principal, project_id, MATRIX_LOGICAL_NAME)
        bundle_restore_needed = bool(
            target_evidence_id
            and (
                current_evidence is None or current_evidence.id != target_evidence_id
            )
        ) or bool(
            target_overlay_id
            and (current_overlay is None or current_overlay.id != target_overlay_id)
        ) or bool(
            target_matrix_id
            and (current_matrix is None or current_matrix.id != target_matrix_id)
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
            if target_matrix_id:
                target_matrix = self.artifacts.resolve_owned_artifact(
                    principal.user_id, target_matrix_id
                )
                if (
                    target_matrix.artifact.project_id != project_id
                    or target_matrix.artifact.logical_name != MATRIX_LOGICAL_NAME
                ):
                    raise WorkflowNotFound("Draft Matrix version not found.")
                files[MATRIX_LOGICAL_NAME] = (
                    target_matrix.path.read_bytes(),
                    target_matrix.artifact.artifact_type,
                )
                if current_matrix is not None:
                    expected_currents[MATRIX_LOGICAL_NAME] = current_matrix.id
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
                    if MATRIX_LOGICAL_NAME in published_so_far:
                        value["source_matrix_artifact_id"] = published_so_far[
                            MATRIX_LOGICAL_NAME
                        ].id
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
                "matrix_artifact_id": (
                    published[MATRIX_LOGICAL_NAME].id
                    if MATRIX_LOGICAL_NAME in published
                    else target_matrix_id
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
        promoted_facts: list[dict[str, Any]] = []
        promoted_fact_ids: set[str] = set()
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
            claim_fact_bindings = [
                dict(value)
                for value in score.get("claim_fact_bindings")
                or entry.get("claim_fact_bindings")
                or []
                if isinstance(value, dict)
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
                    for binding in claim_fact_bindings:
                        if (
                            str(binding.get("paper_id") or "") != paper_id
                            or str(binding.get("source_ref") or "") != source_ref
                            or float(binding.get("confidence") or 0.0) < 0.8
                        ):
                            continue
                        fact_value = " ".join(
                            str(binding.get("value") or "").split()
                        ).strip()
                        support_excerpt = " ".join(
                            str(binding.get("support_excerpt") or "").split()
                        ).strip()
                        if not fact_value or not support_excerpt:
                            continue
                        spans = fact_support_spans(
                            {"value": fact_value, "evidence_key": evidence_key, "support_excerpt": support_excerpt},
                            {evidence_key: {**row, "source_lineage_hash": digest}},
                        )
                        if not spans:
                            continue
                        fact_id = fact_identity({"paper_id": paper_id, "field_id": "claim_targeted_fact",
                            "value": fact_value, "subject": binding.get("subject"), "predicate": binding.get("predicate"),
                            "qualifiers": binding.get("qualifiers") or {}, "epistemic_status": "direct_source_report",
                            "evidence_refs": spans})
                        if fact_id in promoted_fact_ids:
                            continue
                        promoted_fact_ids.add(fact_id)
                        promoted_facts.append(
                            {
                                "paper_id": paper_id,
                                "fact": {
                                    "fact_id": fact_id,
                                    "paper_id": paper_id,
                                    "fact_schema_version": "scientific-fact/2",
                                    "field_id": "claim_targeted_fact",
                                    "fact_type": str(
                                        binding.get("fact_type") or "claim_support"
                                    ),
                                    "subject": str(binding.get("subject") or ""),
                                    "predicate": str(binding.get("predicate") or ""),
                                    "value": fact_value,
                                    "normalized_value": str(
                                        binding.get("normalized_value") or fact_value
                                    ),
                                    "unit": str(binding.get("unit") or ""),
                                    "qualifiers": dict(
                                        binding.get("qualifiers") or {}
                                    ),
                                    "support_excerpt": support_excerpt,
                                    "support_spans": spans,
                                    "epistemic_status": "direct_source_report",
                                    "confidence": round(
                                        float(binding.get("confidence") or 0.0), 4
                                    ),
                                    "human_checked": False,
                                    "review_status": "not_required",
                                    "source_channel": "body",
                                    "support_level": "direct",
                                    "assertion_ceiling": "direct_source_report",
                                    "evidence_refs": [
                                        {
                                            "evidence_key": evidence_key,
                                            "chunk_id": source_ref,
                                            "page_start": passage.get("page"),
                                            "page_end": passage.get("page"),
                                            "source_lineage_hash": digest,
                                        }
                                    ],
                                    "source_span": {
                                        "evidence_key": evidence_key,
                                        "source_type": "body",
                                        "block_id": source_ref,
                                        "page_start": passage.get("page"),
                                        "page_end": passage.get("page"),
                                        "source_lineage_hash": digest,
                                    },
                                    "extraction": {
                                        "method": "draft_targeted_claim_binding",
                                        "prompt_version": "claim-evidence-matching/1",
                                    },
                                    "claim_ids": claim_ids,
                                    "origin_paragraph_id": paragraph_id,
                                },
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

        agent_repair = built.get("fact_agent_repair") or {}
        selected = {
            pid: (scores.get(pid, {}).get("source_evidence_refs")
                  or source_entries.get(pid, {}).get("source_evidence_refs") or [])
            for pid in source_entries.keys() | scores.keys()
        }
        package, agent_summary = attach_repair_fact_context(
            package, agent_repair, selected_by_paragraph=selected,
        )
        promoted_facts.extend(agent_summary["promoted_facts"])
        affected_paragraphs.update(agent_summary["affected_paragraph_ids"])
        affected_sections.update(agent_summary["affected_section_ids"])
        sections = package.get("sections") or []

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
                    plan["status"] = "retrieval_not_found"
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
                        "lexical"
                        if direct_papers
                        else str(section.get("retrieval_mode") or "")
                    ),
                    **({"retrieval_provenance": list(dict.fromkeys([
                        *(section.get("retrieval_provenance") or []),
                        "draft_targeted_source_recheck",
                    ]))} if str(section.get("section_id") or "") in affected_sections else {}),
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

        promoted_papers = {row["paper_id"] for row in agent_summary["promoted_facts"]}
        promoted_sources = [paper for paper in agent_repair.get("fact_repair_sources") or []
                            if str(paper.get("paper_id")) in promoted_papers]
        repaired_at = utc_now().isoformat()
        summary = {
            "status": "completed",
            "repaired_at": repaired_at,
            "added_evidence_count": len(added),
            "downgraded_claim_count": len(dispositions),
            "affected_section_ids": sorted(affected_sections),
            "affected_paragraph_ids": sorted(affected_paragraphs),
            "added_evidence": added,
            "promoted_fact_count": len(promoted_facts),
            "promoted_facts": promoted_facts,
            "pending_fact_corrections": agent_summary["pending_corrections"],
            "fact_agent_source_lineages": {source: lineage
                for paper in promoted_sources
                for source, lineage in (paper.get("source_lineages") or {}).items()},
            "fact_agent_source_parents": {source: str(paper.get("paper_id"))
                for paper in promoted_sources
                for source in paper.get("source_lineages") or {} if source != str(paper.get("paper_id"))},
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
    def _matrix_with_promoted_facts(
        matrix: dict[str, Any], evidence_repair: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Add only hard-validated Draft repair facts to their current paper.

        This helper intentionally cannot alter paper identity, classifications,
        assignments, or an existing Fact's scientific value.  It therefore
        remains safe to publish in the same atomic unit as Evidence/Draft.
        """

        candidate = deepcopy(matrix or {})
        rows = [dict(row) for row in candidate.get("rows") or [] if isinstance(row, dict)]
        by_paper = {
            str(row.get("paper_id") or ""): row
            for row in rows
            if str(row.get("paper_id") or "")
        }
        applied: list[dict[str, Any]] = []
        for promotion in evidence_repair.get("promoted_facts") or []:
            if not isinstance(promotion, dict):
                continue
            paper_id = str(promotion.get("paper_id") or "")
            fact = dict(promotion.get("fact") or {})
            fact_id = str(fact.get("fact_id") or "")
            row = by_paper.get(paper_id)
            if (
                row is None
                or not fact_is_usable(fact)
                or not fact_id
                or str(fact.get("field_id") or "") == "topic_partition"
                or not str(fact.get("support_excerpt") or "").strip()
                or not list(fact.get("evidence_refs") or [])
            ):
                continue
            facts = [
                dict(value)
                for value in row.get("scientific_facts") or []
                if isinstance(value, dict)
            ]
            existing = next(
                (value for value in facts if str(value.get("fact_id") or "") == fact_id),
                None,
            )
            if existing is not None:
                if str(existing.get("value") or "") != str(fact.get("value") or ""):
                    continue
            else:
                merged = merge_facts(facts, [fact])
                if len(merged) != len(facts):
                    row["scientific_facts"] = merged
                    applied.append({"paper_id": paper_id, "fact_id": fact_id})
        candidate["rows"] = rows
        if applied:
            history = [
                dict(value)
                for value in candidate.get("fact_repair_history") or []
                if isinstance(value, dict)
            ]
            history.append(
                {
                    "operation": "draft_targeted_fact_promotion",
                    "promoted_facts": applied,
                    "updated_at": utc_now().isoformat(),
                }
            )
            candidate["fact_repair_history"] = history[-50:]
        return candidate, applied

    @classmethod
    def _single_paragraph_evidence_repair(
        cls,
        job_payload: dict[str, Any],
        candidate_evaluation: dict[str, Any],
        source_paragraph_evaluation: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Build a deterministic Evidence Package patch for one scored candidate.

        Candidate generation already performs a paragraph-scoped MinerU source
        recheck.  Reuse that exact result instead of calling a model again.  The
        returned package is published only if the user accepts the candidate.
        """

        paragraph_score = dict(candidate_evaluation.get("paragraph_score") or {})
        source_entry = candidate_evaluation.get("source_check_entry")
        source_entry = dict(source_entry) if isinstance(source_entry, dict) else {}
        source_score = dict(
            (source_paragraph_evaluation or {}).get("paragraph_score") or {}
        )
        paragraph_id = str(
            candidate_evaluation.get("paragraph_id")
            or paragraph_score.get("paragraph_id")
            or source_score.get("paragraph_id")
            or ""
        )
        repair_input: dict[str, Any] = {
            "fact_agent_repair": candidate_evaluation.get("fact_agent_repair") or {},
            "paragraph_scores": [paragraph_score] if paragraph_score else [],
            "source_check": {
                "entries": [source_entry] if source_entry else [],
            },
            "review_changes": [
                {
                    "paragraph_id": paragraph_id,
                    "unsupported_claims_before": list(
                        source_score.get("unsupported_claims") or []
                    ),
                    "unsupported_claims_after": list(
                        paragraph_score.get("unsupported_claims") or []
                    ),
                    "source_check_status_before": str(
                        source_score.get("source_check_status") or ""
                    ),
                    "source_check_status_after": str(
                        paragraph_score.get("source_check_status") or ""
                    ),
                }
            ],
        }
        return cls._repair_evidence_package(job_payload, repair_input)

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
        paragraph_roles = {
            str(paragraph.get("paragraph_id") or ""): str(
                paragraph.get("argument_role") or ""
            )
            for section in (job_payload.get("writing_plan") or {}).get("sections") or []
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict) and paragraph.get("paragraph_id")
        }
        paragraph_claim_ids = {
            str(paragraph.get("paragraph_id") or ""): [
                str(value)
                for value in paragraph.get("claim_ids") or []
                if str(value)
            ]
            for section in (job_payload.get("writing_plan") or {}).get("sections") or []
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict) and paragraph.get("paragraph_id")
        }
        claims_by_id = {
            str(claim.get("claim_id") or ""): claim
            for section in (job_payload.get("writing_plan") or {}).get("sections") or []
            if isinstance(section, dict)
            for claim in section.get("claims") or []
            if isinstance(claim, dict) and claim.get("claim_id")
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
        routed_issues: list[dict[str, Any]] = []
        for index, raw_issue in enumerate(built.get("issues") or [], 1):
            if not isinstance(raw_issue, dict):
                continue
            issue = dict(raw_issue)
            paragraph_id = str(issue.get("paragraph_id") or "")
            score = paragraph_scores.get(paragraph_id, {})
            issue["finding_category"] = score.get("finding_category") or issue.get("finding_category") or "unknown"
            if not issue.get("rule") and not issue.get("failed_dimensions"):
                issue["failed_dimensions"] = list(score.get("failed_dimensions") or [])
            issue["missing_core_claim_ids"] = list(score.get("missing_core_claim_ids") or issue.get("missing_core_claim_ids") or [])
            section_id = paragraph_sections.get(paragraph_id, "")
            section_evidence = evidence_sections.get(section_id, {})
            source_status = str(
                score.get("source_check_status")
                or issue.get("source_check_status")
                or "not_assessed"
            ).casefold()
            route = str(score.get("route") or issue.get("route") or "").casefold()
            source_entry = source_checks.get(paragraph_id, {})
            issue_claim_ids = [
                str(value)
                for value in (
                    issue.get("claim_ids")
                    or ([issue.get("claim_id")] if issue.get("claim_id") else [])
                    or paragraph_claim_ids.get(paragraph_id, [])
                )
                if str(value)
            ]
            issue["claim_ids"] = list(dict.fromkeys(issue_claim_ids))
            issue["core_claim_ids"] = [cid for cid in issue_claim_ids
                                       if claims_by_id.get(cid, {}).get("required_for_section")]
            for key in ("unsupported_claims", "evidence_rescue_status", "observed_problem",
                        "source_attribution_repair"):
                if key in score:
                    issue[key] = score[key]
            issue["paper_ids"] = quality_issue_paper_ids(
                issue,
                source_entry=source_entry,
                claims_by_id=claims_by_id,
                paragraph_claim_ids=paragraph_claim_ids.get(paragraph_id, []),
            )
            issue["source_evidence_refs"] = quality_issue_source_evidence_refs(
                issue,
                score=score,
                source_entry=source_entry,
            )
            has_original_passages = any(
                paper.get("passages")
                for paper in source_entry.get("papers") or []
                if isinstance(paper, dict)
            )
            reference_map_problem = any(
                marker
                in " ".join(
                    [
                        route,
                        source_status,
                        *[
                            str(value)
                            for value in score.get("failed_dimensions")
                            or issue.get("failed_dimensions")
                            or []
                        ],
                        str(issue.get("diagnosis") or issue.get("message") or ""),
                    ]
                ).casefold()
                for marker in (
                    "citation_reference_map_mismatch",
                    "citation-map mismatch",
                    "citation map mismatch",
                    "unlisted bibliography",
                    "callout",
                )
            )
            repair = route_draft_issue(
                {
                    **issue,
                    "section_id": section_id,
                    "paragraph_role": paragraph_roles.get(paragraph_id, ""),
                    "failed_dimensions": list(
                        score.get("failed_dimensions")
                        or issue.get("failed_dimensions")
                        or []
                    ),
                    "paper_ids": list(issue.get("paper_ids") or []),
                },
                source_status=source_status,
                evaluator_route=route,
                has_original_passages=has_original_passages,
                reference_map_problem=reference_map_problem,
                source_evidence_refs=list(
                    issue.get("source_evidence_refs") or []
                ),
                source_ready=has_original_passages,
                evidence_texts=[
                    str(passage.get("text") or "")
                    for paper in source_entry.get("papers") or []
                    if isinstance(paper, dict)
                    for passage in paper.get("passages") or []
                    if isinstance(passage, dict) and str(passage.get("text") or "").strip()
                ],
            )
            actual_stage = str(repair.get("repair_stage") or "draft")
            if repair.get("repair_class") == "planning_adjustment":
                issue["planning_claims"] = [{"claim_id": cid,
                    "original_text": str(claims_by_id.get(cid, {}).get("proposition") or claims_by_id.get(cid, {}).get("claim") or "")}
                    for cid in issue_claim_ids]
            # The existing Draft page can execute local Evidence and
            # bibliography repairs itself.  Keep navigation compatibility
            # while exposing the precise workflow owner in ``repair_stage``.
            stage = DraftsService._navigation_stage_for_repair(str(repair.get("execution_stage") or actual_stage))
            action = str(repair.get("recommended_action") or "")
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
                    "paragraph_role": paragraph_roles.get(paragraph_id, ""),
                    "source_check_status": source_status,
                    "source_evidence_refs": list(
                        issue.get("source_evidence_refs") or []
                    ),
                    "recommended_return_stage": stage,
                    "recommended_action": action,
                    **repair,
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
        return routed_issues, DraftsService._routing_summary_from_issues(routed_issues)

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
            elif route == "planning_revision":
                scope = section_id or "unassigned-section"
            else:
                scope = str(issue.get("paragraph_id") or issue_id or "unknown")
            # Route is an action, not the identity of a scientific defect.
            family = "paragraph_issue" if route in {
                "paragraph_rewrite", "targeted_evidence_then_paragraph_rewrite",
                "claim_downgrade_then_paragraph_rewrite",
            } else route
            key = f"{family}:{scope}"
            if scope != "global" and route != "planning_revision":
                # Distinct missing claims/sources in one paragraph must not
                # disappear behind a single generic rewrite root.
                targets = {"claims": sorted(set(str(value) for value in issue.get("claim_ids") or [])),
                           "papers": sorted(set(str(value) for value in [
                               *(issue.get("paper_ids") or []), *(issue.get("unresolved_primary_papers") or [])]))}
                if any(targets.values()):
                    key += ":" + json.dumps(targets, sort_keys=True)
            root = grouped.setdefault(
                key,
                {
                    "root_cause_id": "ROOT-"
                    + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper(),
                    "repair_route": route,
                    "scope": scope,
                    "issue_type": str(issue.get("issue_type") or "draft_wording"),
                    "auto_repairable": bool(issue.get("auto_repairable", True)),
                    "repair_class": issue.get("repair_class"),
                    "resolution_state": issue.get("resolution_state", "open"),
                    "requires_user_decision": requires_user_decision(issue),
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
            root["requires_user_decision"] = bool(
                root["requires_user_decision"]
            ) or requires_user_decision(issue)
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
                "repair_class": root.get("repair_class"),
                "resolution_state": root.get("resolution_state", "open"),
                "target": {
                    "paragraph_ids": root["paragraph_ids"],
                    "section_ids": root["section_ids"],
                    "paper_ids": root["paper_ids"],
                },
                "status": (
                    root["resolution_state"] if root.get("resolution_state", "open") != "open" else
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
    def _routing_summary_from_issues(
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Rebuild the routing aggregate after a paragraph-only evaluation."""

        priority = {"discovery": 0, "planning": 1, "sections": 2, "draft": 3}
        labels = {
            "discovery": "Return to literature retrieval",
            "planning": "Return to Matrix and outline",
            "sections": "Repair the affected synthesis or writing plan",
            "draft": "Revise wording in the current Draft",
        }
        by_stage: dict[str, list[str]] = {stage: [] for stage in priority}
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            stage = str(issue.get("recommended_return_stage") or "draft")
            if stage not in by_stage:
                stage = "draft"
            issue_id = str(issue.get("issue_id") or issue.get("id") or "")
            if issue_id:
                by_stage[stage].append(issue_id)
        active = [stage for stage, values in by_stage.items() if values]
        recommended = min(active, key=priority.get) if active else "draft"
        return {
            "recommended_return_stage": recommended,
            "recommended_action": labels[recommended],
            "issues_by_stage": by_stage,
            "counts_by_stage": {
                stage: len(issue_ids) for stage, issue_ids in by_stage.items()
            },
        }

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
        def same_legacy_target(old, current):
            # Old immutable reports hashed the action route into the root ID.
            # A routing upgrade alone must not count as a scientific repair.
            prose_routes = {"paragraph_rewrite", "targeted_evidence_then_paragraph_rewrite",
                            "claim_downgrade_then_paragraph_rewrite"}
            return bool(old.get("repair_route") in prose_routes
                        and current.get("repair_route") in prose_routes
                        and old.get("paragraph_ids")
                        and set(old.get("paragraph_ids") or []) == set(current.get("paragraph_ids") or [])
                        and set(old.get("paper_ids") or []) == set(current.get("paper_ids") or []))

        retained_legacy_ids = {
            str(old.get("root_cause_id") or "")
            for old in source_quality.get("root_causes") or []
            if isinstance(old, dict) and any(same_legacy_target(old, row) for row in current_roots)
        }
        resolved_ids = sorted(source_ids - remaining_ids - retained_legacy_ids)
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
            or evidence_repair_has_changes(evidence_repair)
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
            repair_status = "requires_user_input" if remaining_user else "unchanged"
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
        scoring = {str(change.get("paragraph_id") or ""): change for change in selected_changes}
        for change in selected_changes:
            for pid, evaluation in (change.get("dependent_evaluations") or {}).items():
                scoring.setdefault(pid, {"paragraph_id": pid, "candidate_evaluation": evaluation})
        scored_ids = set()
        for change in scoring.values():
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
                scored_ids.add(paragraph_id)
        scored_changes = sum(str(change.get("paragraph_id") or "") in scored_ids for change in selected_changes)
        if scored_changes == len(selected_changes) and selected_changes:
            # Avoid accumulating one rounding operation per paragraph.  The
            # comparison UI sums unrounded overall deltas once, so publish the
            # same deterministic total here.
            source_quality = dict(
                proposal.get("source_quality")
                or proposal.get("candidate_quality")
                or {}
            )
            source_score = quality_score(source_quality)
            explicit_deltas: list[float] = []
            for change in scoring.values():
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
                for change in scoring.values():
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
            or PARAGRAPH_PASS_THRESHOLD
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
            previous_issue = next(
                (
                    dict(item)
                    for item in current_quality.get("issues") or []
                    if isinstance(item, dict)
                    and str(item.get("paragraph_id") or "") == paragraph_id
                ),
                {},
            )
            issue = {
                **previous_issue,
                **paragraph_score,
                "issue_id": f"incremental-{paragraph_id}-{uuid.uuid4().hex[:8]}",
                "message": str(
                    paragraph_score.get("diagnosis") or "Review this paragraph."
                ),
            }
            source_entry = built.get("source_check_entry")
            source_entry = source_entry if isinstance(source_entry, dict) else {}
            has_original_passages = any(
                paper.get("passages")
                for paper in source_entry.get("papers") or []
                if isinstance(paper, dict)
            )
            source_status = str(
                paragraph_score.get("source_check_status")
                or issue.get("source_check_status")
                or "not_assessed"
            )
            evaluator_route = str(
                paragraph_score.get("route") or issue.get("route") or ""
            )
            issue.update(
                route_draft_issue(
                    issue,
                    source_status=source_status,
                    evaluator_route=evaluator_route,
                    has_original_passages=has_original_passages,
                    reference_map_problem=(
                        str(issue.get("issue_type") or "")
                        == "citation_reference_mapping"
                    ),
                    source_evidence_refs=list(
                        paragraph_score.get("source_evidence_refs")
                        or issue.get("source_evidence_refs")
                        or []
                    ),
                    source_ready=has_original_passages,
                    evidence_texts=[
                        str(passage.get("text") or "")
                        for paper in source_entry.get("papers") or []
                        if isinstance(paper, dict)
                        for passage in paper.get("passages") or []
                        if isinstance(passage, dict)
                        and str(passage.get("text") or "").strip()
                    ],
                )
            )
            repair_stage = str(issue.get("repair_stage") or "draft")
            issue["recommended_return_stage"] = self._navigation_stage_for_repair(str(issue.get("execution_stage") or repair_stage))
            issues.append(issue)
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
        for finding in preflight.get("paragraph_findings") or []:
            if isinstance(finding, dict):
                finding["hard_gate"] = paragraph_finding_is_blocking(finding)
        if any(
            paragraph_finding_is_blocking(item)
            for item in preflight.get("paragraph_findings") or []
            if isinstance(item, dict)
        ):
            hard_gate_failures.add("paragraph_readability_or_source_failures")
        hard_gate_failures = sorted(hard_gate_failures)
        goal = float(current_quality.get("goal") or current_quality.get("pass_threshold") or DRAFT_PASS_THRESHOLD)
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
        root_causes, repair_tasks = self._quality_root_causes(issues)
        routing = self._routing_summary_from_issues(issues)
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
            "routing": routing,
            "root_causes": root_causes,
            "repair_tasks": repair_tasks,
            "repair_summary": self._repair_summary(
                current_quality, root_causes
            ),
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
