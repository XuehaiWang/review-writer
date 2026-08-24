"""PostgreSQL-native source review, redraw, SVG editing, and figure gates."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import Image as PILImage, UnidentifiedImageError

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.figure_rules import (
    aspect_ratio_integrity,
    append_operation_overlays,
    build_full_vector_svg,
    canvas_policy_matches,
    image_size,
    png_size,
    svg_workspace_size,
    validate_svg_markup,
    validated_content_crop,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.publication_caption import (
    canonical_figure_role,
    infer_figure_role,
    publication_caption_fields,
)


SECTION_INDEX = "sections/section_drafts.json"
PAPER_CANDIDATES = "sections/paper_figure_candidates.json"
FIGURE_CANDIDATES = "sections/figure_candidates.json"
DEFAULT_REVIEWS = "sections/default_figure_reviews.json"
REVIEW_SELECTIONS = "figure-review/selections.json"
REVIEW_INPUTS = "figure-review/selected_figures.json"
FIGURE_MANIFEST = "figures/manifest.json"
MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
PNG_DATA_URL = re.compile(r"^data:image/png;base64,(.+)$", re.DOTALL)
SAFETY_ERROR = re.compile(
    r"adult content|sexual content|safety policy|safety review|moderation",
    re.IGNORECASE,
)


def artifact_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{artifact_id}/content"


def _edge_check(path: Path, *, safe_margin_px: int = 8) -> dict[str, Any]:
    """Detect dark/color ink touching a raster boundary without changing the image."""

    with PILImage.open(path) as image:
        grayscale = image.convert("L")
        mask = grayscale.point(lambda value: 255 if value < 235 else 0)
        bbox = mask.getbbox()
        width, height = image.size
    if bbox is None:
        return {
            "status": "warning",
            "ink_touches_edge": False,
            "margin_px": None,
            "reason": "no_detectable_ink",
        }
    left, top, right, bottom = bbox
    margin = min(left, top, width - right, height - bottom)
    return {
        "status": "warning" if margin < safe_margin_px else "pass",
        "ink_touches_edge": margin < safe_margin_px,
        "margin_px": int(margin),
        "safe_margin_px": int(safe_margin_px),
        "ink_bbox": [int(left), int(top), int(right), int(bottom)],
    }


class FigureCandidatesMissing(WorkflowConflict):
    code = "FIGURE_CANDIDATES_MISSING"


class FigureParagraphAnchorMissing(WorkflowConflict):
    code = "FIGURE_PARAGRAPH_ANCHOR_MISSING"


class FigureReviewIncomplete(WorkflowConflict):
    code = "FIGURE_REVIEW_INCOMPLETE"


class FigureOutputsIncomplete(WorkflowConflict):
    code = "FIGURE_OUTPUTS_INCOMPLETE"


class FigureCanvasMismatch(WorkflowValidationError):
    code = "FIGURE_CANVAS_MISMATCH"


class FigureSafetyBlocked(WorkflowConflict):
    code = "FIGURE_SAFETY_BLOCKED"


class FigureOutputUnavailable(WorkflowConflict):
    code = "FIGURE_OUTPUT_UNAVAILABLE"


class FiguresService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
    ):
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
    ) -> tuple[Any, ArtifactRecord | None]:
        self._owned_project(principal, project_id)
        artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )
        if artifact is None:
            if required:
                raise FigureCandidatesMissing(
                    "Current section figure candidates are unavailable. Regenerate Sections first.",
                    details={"logical_name": logical_name},
                )
            return None, None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowValidationError(
                "A current figure artifact is unreadable.",
                details={"artifact_id": artifact.id, "logical_name": logical_name},
            ) from exc
        return payload, artifact

    def _artifact_path(
        self,
        principal: Principal,
        project_id: str,
        artifact_id: str,
    ) -> tuple[ArtifactRecord, Path]:
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact_id)
        if resolved.artifact.project_id != project_id:
            raise WorkflowNotFound("Figure artifact not found.")
        return resolved.artifact, resolved.path

    @staticmethod
    def _papers(payload: Any) -> list[dict[str, Any]]:
        rows = payload.get("papers") if isinstance(payload, dict) else None
        return [dict(row) for row in rows or [] if isinstance(row, dict)]

    def _paper_display_labels(
        self,
        principal: Principal,
        project_id: str,
        fallback_ids: list[str],
    ) -> dict[str, str]:
        matrix, _artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME, required=False
        )
        rows = matrix.get("rows") if isinstance(matrix, dict) else []
        ordered = [
            str(row.get("paper_id") or "").strip()
            for row in rows or []
            if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
        ]
        ordered.extend(str(value or "").strip() for value in fallback_ids)
        ordered = list(dict.fromkeys(value for value in ordered if value))
        width = max(3, len(str(len(ordered))))
        return {
            paper_id: f"P{index:0{width}d}"
            for index, paper_id in enumerate(ordered, start=1)
        }

    @staticmethod
    def _candidate(rows: list[dict[str, Any]], paper_id: str, index: int):
        paper = next(
            (row for row in rows if str(row.get("paper_id") or "") == paper_id),
            None,
        )
        if not isinstance(paper, dict):
            raise WorkflowNotFound("Paper figure candidates were not found.")
        candidate = next(
            (
                item
                for item in paper.get("candidates") or []
                if isinstance(item, dict)
                and not isinstance(item.get("candidate_index"), bool)
                and item.get("candidate_index") == index
            ),
            None,
        )
        if not isinstance(candidate, dict):
            raise WorkflowValidationError(
                "Selected candidate does not belong to this paper."
            )
        return paper, dict(candidate)

    def _validate_candidate(
        self,
        principal: Principal,
        project_id: str,
        candidate: dict[str, Any],
    ) -> tuple[ArtifactRecord, Path]:
        artifact_id = str(candidate.get("source_image_artifact_id") or "").strip()
        if not artifact_id:
            raise WorkflowValidationError(
                "The selected candidate has no immutable source image artifact."
            )
        artifact, path = self._artifact_path(
            principal, project_id, artifact_id
        )
        try:
            image_size(path)
        except (OSError, UnidentifiedImageError) as exc:
            raise WorkflowValidationError(
                "The selected candidate source image is unreadable."
            ) from exc
        return artifact, path

    def _derive_current_placement(
        self,
        principal: Principal,
        project_id: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive manuscript placement from the current paper-to-paragraph evidence map."""

        paper_id = str(candidate.get("paper_id") or "").strip()
        section_index, _artifact = self._read_json(
            principal, project_id, SECTION_INDEX
        )
        role = infer_figure_role(
            candidate.get("source_caption_text"),
            candidate.get("what_it_shows"),
            candidate.get("why_selected"),
            preferred=candidate.get("representative_role"),
        )
        role_terms = {
            "workflow": ("workflow", "strategy", "study design", "pipeline", "method"),
            "core_transformation": ("reaction", "transformation", "synthesis", "conditions", "route"),
            "mechanism_model": ("mechanism", "pathway", "intermediate", "transition state", "catalytic cycle"),
            "scope_samples": ("scope", "substrate", "sample", "generality", "tolerance"),
            "quantitative_results": ("result", "yield", "selectivity", "performance", "kinetic", "trend"),
            "comparison_ablation": ("comparison", "benchmark", "control", "versus", "ablation"),
            "conceptual_overview": ("overview", "concept", "introduction", "strategy", "classification"),
            "structure_image": ("structure", "crystal", "microscopy", "morphology", "imaging"),
            "unknown": (),
        }.get(role, ())
        matches: list[dict[str, Any]] = []
        for section in section_index.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            section_heading = str(section.get("heading") or section.get("title") or "")
            for paragraph in section.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                cited = {
                    str(value)
                    for value in (
                        paragraph.get("cited_paper_ids")
                        or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
                    )
                    if str(value)
                }
                cited.update(
                    str(item.get("paper_id") or "")
                    for item in paragraph.get("evidence") or []
                    if isinstance(item, dict) and str(item.get("paper_id") or "")
                )
                paragraph_id = str(paragraph.get("paragraph_id") or "").strip()
                if paper_id and paragraph_id and paper_id in cited:
                    paragraph_text = " ".join(
                        str(paragraph.get(key) or "")
                        for key in ("text", "paragraph_text", "summary", "purpose")
                    )
                    realization_text = " ".join(
                        " ".join(
                            str(realization.get(key) or "")
                            for key in ("claim_text", "text", "intent", "claim_type")
                        )
                        for realization in paragraph.get("claim_realizations") or []
                        if isinstance(realization, dict)
                    )
                    searchable = f"{section_heading} {paragraph_text} {realization_text}".casefold()
                    evidence_ids = list(
                        dict.fromkeys(
                            str(ref.get("evidence_id") or "")
                            for realization in paragraph.get("claim_realizations") or []
                            if isinstance(realization, dict)
                            and paper_id
                            in {
                                str(value)
                                for value in realization.get("citation_group") or []
                            }
                            for ref in realization.get("evidence_refs") or []
                            if isinstance(ref, dict)
                            and str(ref.get("evidence_id") or "")
                        )
                    )
                    claim_ids = list(
                        dict.fromkeys(
                            str(realization.get("claim_id") or "")
                            for realization in paragraph.get("claim_realizations") or []
                            if isinstance(realization, dict)
                            and str(realization.get("claim_id") or "")
                            and paper_id
                            in {
                                str(value)
                                for value in realization.get("citation_group") or []
                            }
                        )
                    )
                    semantic_matches = [term for term in role_terms if term in searchable]
                    original_target = str(
                        candidate.get("target_paragraph_id")
                        or candidate.get("paragraph_id")
                        or ""
                    )
                    score = len(semantic_matches) * 10
                    score += 3 if claim_ids else 0
                    score += min(3, len(evidence_ids))
                    score += 1 if paragraph_id == original_target else 0
                    matches.append(
                        {
                            "section_id": section_id,
                            "section_heading": section_heading,
                            "paragraph_id": paragraph_id,
                            "evidence_ids": evidence_ids,
                            "claim_ids": claim_ids,
                            "score": score,
                            "semantic_matches": semantic_matches,
                        }
                    )
        updated = dict(candidate)
        updated["source_target_paragraph_id"] = str(
            candidate.get("target_paragraph_id") or candidate.get("paragraph_id") or ""
        )
        if matches:
            matches.sort(
                key=lambda item: (
                    -int(item["score"]),
                    str(item["section_id"]),
                    str(item["paragraph_id"]),
                )
            )
            chosen = matches[0]
            updated["section_id"] = chosen["section_id"]
            updated["section_heading"] = chosen["section_heading"]
            updated["target_paragraph_id"] = chosen["paragraph_id"]
            updated["evidence_ids"] = list(chosen["evidence_ids"])
            updated["claim_ids"] = list(chosen["claim_ids"])
            updated["representative_role"] = role
            updated["placement_status"] = "semantic_role_matched"
            updated["placement_reason"] = (
                f"role={role}; matched="
                + (", ".join(chosen["semantic_matches"]) or "evidence_tie_break")
            )
            updated["placement_candidate_paragraph_ids"] = [
                str(match["paragraph_id"]) for match in matches
            ]
        else:
            updated["target_paragraph_id"] = ""
            updated["evidence_ids"] = []
            updated["claim_ids"] = []
            updated["placement_status"] = "waiting_for_supported_paragraph"
            updated["placement_reason"] = f"role={role}; no paragraph cites this paper"
            updated["placement_candidate_paragraph_ids"] = []
        return updated

    def _effective_review(
        self,
        principal: Principal,
        project_id: str,
        paper_artifact: ArtifactRecord,
    ) -> tuple[dict[str, Any], ArtifactRecord | None, bool]:
        defaults, _defaults_artifact = self._read_json(
            principal, project_id, DEFAULT_REVIEWS, required=False
        )
        selections, selections_artifact = self._read_json(
            principal, project_id, REVIEW_SELECTIONS, required=False
        )
        stale = bool(
            selections_artifact
            and (
                not isinstance(selections, dict)
                or selections.get("source_paper_candidates_artifact_id")
                != paper_artifact.id
            )
        )
        source = defaults if isinstance(defaults, dict) else {"papers": {}}
        if selections_artifact and not stale and isinstance(selections, dict):
            source = selections
        reviews = deepcopy(source)
        if not isinstance(reviews.get("papers"), dict):
            reviews["papers"] = {}
        return reviews, selections_artifact if not stale else None, stale

    def get_review(self, principal: Principal, project_id: str) -> dict[str, Any]:
        paper_payload, paper_artifact = self._read_json(
            principal, project_id, PAPER_CANDIDATES
        )
        self._read_json(principal, project_id, SECTION_INDEX)
        papers = self._papers(paper_payload)
        reviews, selections_artifact, stale = self._effective_review(
            principal, project_id, paper_artifact
        )
        review_rows = reviews.get("papers") or {}
        visible: list[dict[str, Any]] = []
        for raw_paper in papers:
            paper = dict(raw_paper)
            candidates: list[dict[str, Any]] = []
            for raw_candidate in paper.get("candidates") or []:
                if not isinstance(raw_candidate, dict):
                    continue
                candidate = dict(raw_candidate)
                artifact_id = str(candidate.get("source_image_artifact_id") or "")
                if artifact_id:
                    try:
                        self._artifact_path(principal, project_id, artifact_id)
                    except (WorkflowNotFound, WorkflowValidationError):
                        candidate["source_image_url"] = ""
                    else:
                        candidate["source_image_url"] = artifact_url(artifact_id)
                        candidate["source_image_path"] = candidate["source_image_url"]
                candidates.append(candidate)
            paper["candidates"] = candidates
            paper["review_required"] = any(
                bool(candidate.get("source_image_url")) for candidate in candidates
            )
            paper_id = str(paper.get("paper_id") or "")
            review = (
                dict(review_rows.get(paper_id) or {})
                if isinstance(review_rows, dict)
                else {}
            )
            selected_index = review.get("selected_candidate_index")
            if isinstance(selected_index, int) and not isinstance(selected_index, bool):
                paper["selected_candidate_index"] = selected_index
            paper["human_review"] = review
            visible.append(paper)
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "figure-review"
        )
        return {
            "project_id": project_id,
            "papers": visible,
            "paper_display_labels": self._paper_display_labels(
                principal,
                project_id,
                [str(row.get("paper_id") or "") for row in visible],
            ),
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "source_paper_candidates_artifact_id": paper_artifact.id,
            "selection_artifact_id": selections_artifact.id
            if selections_artifact
            else None,
            "freshness": {
                "source_stale": False,
                "review_stale": stale,
                "stale": stale,
            },
        }

    def _publish_files(
        self,
        principal: Principal,
        project_id: str,
        *,
        stage_id: str,
        files: dict[str, tuple[bytes, str]],
        expected_revision: int,
        status: str,
        invalidate_stages: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
        approval_events: list[dict[str, Any]] | None = None,
        expected_current_artifacts: dict[str, str] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            stage_id,
            status="succeeded",
            input_snapshot=dict(metadata or {}),
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
                metadata=dict(metadata or {}),
            )
        state = self.repository.promote_stage_artifacts_atomically(
            principal.user_id,
            project_id,
            stage_id,
            artifact_ids={key: item.id for key, item in published.items()},
            run_id=run.id,
            expected_revision=int(expected_revision),
            status=status,
            invalidate_stages=invalidate_stages,
            approval_events=approval_events,
            expected_current_artifacts=expected_current_artifacts,
        )
        return published, state

    def _selected_review_rows(
        self,
        principal: Principal,
        project_id: str,
        papers: list[dict[str, Any]],
        reviews: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        review_rows = reviews.get("papers") or {}
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for paper in papers:
            paper_id = str(paper.get("paper_id") or "")
            if not paper_id:
                continue
            review = review_rows.get(paper_id) if isinstance(review_rows, dict) else None
            selected_index = (
                review.get("selected_candidate_index")
                if isinstance(review, dict)
                else None
            )
            if isinstance(selected_index, bool) or not isinstance(selected_index, int):
                reviewable = any(
                    isinstance(candidate, dict)
                    and bool(candidate.get("source_image_artifact_id"))
                    for candidate in paper.get("candidates") or []
                )
                if reviewable:
                    missing.append(paper_id)
                continue
            _paper, candidate = self._candidate(papers, paper_id, selected_index)
            candidate["representative_role"] = infer_figure_role(
                candidate.get("source_caption_text"),
                candidate.get("what_it_shows"),
                preferred=(review or {}).get("representative_role"),
            )
            candidate = self._derive_current_placement(
                principal, project_id, candidate
            )
            candidate.update(
                publication_caption_fields(
                    candidate.get("source_caption_text"),
                    representative_role=candidate.get("representative_role"),
                    source_label=candidate.get("source_label"),
                    context_title=candidate.get("section_heading"),
                )
            )
            source_artifact, _source_path = self._validate_candidate(
                principal, project_id, candidate
            )
            candidate["source_image_artifact_id"] = source_artifact.id
            candidate["source_review_note"] = str(review.get("review_note") or "")
            selected.append(candidate)
        return selected, missing

    def _publish_review_inputs(
        self,
        principal: Principal,
        project_id: str,
        *,
        reviews: dict[str, Any],
        selected: list[dict[str, Any]],
        paper_artifact: ArtifactRecord,
        revision: int,
        status: str,
    ) -> tuple[ArtifactRecord, ArtifactRecord, Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            "figure-review",
            status="succeeded",
            input_snapshot={"source_paper_candidates_artifact_id": paper_artifact.id},
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        selections_path = staging / "selections.json"
        selections_path.write_text(
            json.dumps(reviews, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        selection_artifact = self.artifacts.publish(
            principal.user_id,
            project_id,
            run.id,
            selections_path.name,
            logical_name=REVIEW_SELECTIONS,
            artifact_type="json",
            producer_stage="figure-review",
            make_current=False,
        )
        inputs = {
            "project_id": project_id,
            "source_paper_candidates_artifact_id": paper_artifact.id,
            "source_selection_artifact_id": selection_artifact.id,
            "selected_at": utc_now().isoformat(),
            "figures": selected,
        }
        inputs_path = staging / "selected-figures.json"
        inputs_path.write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        inputs_artifact = self.artifacts.publish(
            principal.user_id,
            project_id,
            run.id,
            inputs_path.name,
            logical_name=REVIEW_INPUTS,
            artifact_type="json",
            producer_stage="figure-review",
            make_current=False,
            metadata={
                "source_paper_candidates_artifact_id": paper_artifact.id,
                "source_selection_artifact_id": selection_artifact.id,
            },
        )
        state = self.repository.promote_stage_artifacts_atomically(
            principal.user_id,
            project_id,
            "figure-review",
            artifact_ids={
                REVIEW_SELECTIONS: selection_artifact.id,
                REVIEW_INPUTS: inputs_artifact.id,
            },
            run_id=run.id,
            expected_revision=int(revision),
            status=status,
            invalidate_stages=("figures", "draft", "final"),
        )
        return selection_artifact, inputs_artifact, state

    def save_review_selection(
        self,
        principal: Principal,
        project_id: str,
        paper_id: str,
        *,
        revision: int,
        candidate_index: int,
        review_note: str,
        representative_role: str = "unknown",
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        paper_payload, paper_artifact = self._read_json(
            principal, project_id, PAPER_CANDIDATES
        )
        papers = self._papers(paper_payload)
        _paper, candidate = self._candidate(papers, paper_id, candidate_index)
        source_artifact, _source_path = self._validate_candidate(
            principal, project_id, candidate
        )
        reviews, _current, _stale = self._effective_review(
            principal, project_id, paper_artifact
        )
        reviews["project_id"] = project_id
        reviews["source_paper_candidates_artifact_id"] = paper_artifact.id
        reviews["updated_at"] = utc_now().isoformat()
        review_rows = reviews.setdefault("papers", {})
        review_rows[paper_id] = {
            "selected_candidate_index": int(candidate_index),
            "selected_source_artifact_id": source_artifact.id,
            "review_note": str(review_note or "").strip(),
            "representative_role": canonical_figure_role(representative_role),
            "selection_source": "human",
            "reviewed_at": utc_now().isoformat(),
        }
        selected, missing = self._selected_review_rows(
            principal, project_id, papers, reviews
        )
        selection_complete = bool(selected and not missing)
        with self._write_lock:
            selection_artifact, inputs_artifact, state = self._publish_review_inputs(
                principal,
                project_id,
                reviews=reviews,
                selected=selected,
                paper_artifact=paper_artifact,
                revision=revision,
                status="approved" if selection_complete else "review",
            )
        return {
            "project_id": project_id,
            "paper_id": paper_id,
            "candidate_index": candidate_index,
            "revision": state.revision,
            "status": state.status,
            "selection_artifact_id": selection_artifact.id,
            "selected_figures_artifact_id": inputs_artifact.id,
            "selected_count": len(selected),
            "missing_paper_ids": missing,
            "selection_complete": selection_complete,
        }

    def confirm_review(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        paper_payload, paper_artifact = self._read_json(
            principal, project_id, PAPER_CANDIDATES
        )
        papers = self._papers(paper_payload)
        reviews, _selection_artifact, _stale = self._effective_review(
            principal, project_id, paper_artifact
        )
        selected, missing = self._selected_review_rows(
            principal, project_id, papers, reviews
        )
        if missing:
            raise FigureReviewIncomplete(
                "Select one anchored source image for every cited paper before continuing.",
                details={"paper_ids": missing},
            )
        if not selected:
            raise FigureReviewIncomplete(
                "No manuscript source figure is selected."
            )
        reviews = deepcopy(reviews)
        reviews["project_id"] = project_id
        reviews["source_paper_candidates_artifact_id"] = paper_artifact.id
        with self._write_lock:
            _selection_artifact, inputs_artifact, state = self._publish_review_inputs(
                principal,
                project_id,
                reviews=reviews,
                selected=selected,
                paper_artifact=paper_artifact,
                revision=revision,
                status="approved",
            )
        return {
            "project_id": project_id,
            "status": state.status,
            "revision": state.revision,
            "selected_count": len(selected),
            "selected_figures_artifact_id": inputs_artifact.id,
            "next_tab": "redraw",
        }

    def sync_review_inputs(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Materialize the effective live selection for redraw without requiring completeness."""
        principal.require(Permission.PROJECT_WRITE)
        paper_payload, paper_artifact = self._read_json(
            principal, project_id, PAPER_CANDIDATES
        )
        papers = self._papers(paper_payload)
        reviews, _selection_artifact, _stale = self._effective_review(
            principal, project_id, paper_artifact
        )
        selected, missing = self._selected_review_rows(
            principal, project_id, papers, reviews
        )
        if not selected:
            raise FigureReviewIncomplete(
                "Select at least one anchored source image before opening AI redraw."
            )
        selection_complete = not missing
        current_inputs, current_inputs_artifact = self._read_json(
            principal, project_id, REVIEW_INPUTS, required=False
        )
        current_figures = (
            current_inputs.get("figures")
            if isinstance(current_inputs, dict)
            else None
        )
        source_matches = bool(
            isinstance(current_inputs, dict)
            and current_inputs.get("source_paper_candidates_artifact_id")
            == paper_artifact.id
        )
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "figure-review"
        )
        if (
            current_inputs_artifact is not None
            and source_matches
            and current_figures == selected
        ):
            return {
                "project_id": project_id,
                "status": state.status if state else "review",
                "revision": state.revision if state else int(revision),
                "selected_count": len(selected),
                "missing_paper_ids": missing,
                "selection_complete": selection_complete,
                "selected_figures_artifact_id": current_inputs_artifact.id,
                "next_tab": "redraw",
                "unchanged": True,
            }
        reviews = deepcopy(reviews)
        reviews["project_id"] = project_id
        reviews["source_paper_candidates_artifact_id"] = paper_artifact.id
        with self._write_lock:
            _selection_artifact, inputs_artifact, state = self._publish_review_inputs(
                principal,
                project_id,
                reviews=reviews,
                selected=selected,
                paper_artifact=paper_artifact,
                revision=revision,
                status="approved" if selection_complete else "review",
            )
        return {
            "project_id": project_id,
            "status": state.status,
            "revision": state.revision,
            "selected_count": len(selected),
            "missing_paper_ids": missing,
            "selection_complete": selection_complete,
            "selected_figures_artifact_id": inputs_artifact.id,
            "next_tab": "redraw",
            "unchanged": False,
        }

    def _selected_inputs(
        self, principal: Principal, project_id: str
    ) -> tuple[list[dict[str, Any]], ArtifactRecord]:
        payload, artifact = self._read_json(principal, project_id, REVIEW_INPUTS)
        figures = payload.get("figures") if isinstance(payload, dict) else None
        rows = [dict(row) for row in figures or [] if isinstance(row, dict)]
        if not rows:
            raise FigureReviewIncomplete("No selected source figures are available.")
        return rows, artifact

    def redraw_job_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        figure_ids: list[str],
        figure_type: str,
        retry_of_job_id: str | None = None,
        origin: str = "batch",
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        figures, inputs_artifact = self._selected_inputs(principal, project_id)
        available = {str(row.get("figure_id") or ""): row for row in figures}
        requested = list(dict.fromkeys(str(item).strip() for item in figure_ids if str(item).strip()))
        if not requested:
            current_manifest, _manifest_artifact = self._current_manifest(
                principal, project_id, inputs_artifact.id
            )
            excluded_ids = {
                str(value)
                for value in current_manifest.get("excluded_figure_ids") or []
                if str(value)
            }
            requested = [
                figure_id for figure_id in available if figure_id not in excluded_ids
            ]
        unknown = sorted(set(requested) - set(available))
        if unknown:
            raise WorkflowValidationError(
                "One or more requested figures are not in the current source selection.",
                details={"figure_ids": unknown},
            )
        if retry_of_job_id:
            source_job = self.repository.get_job(principal.user_id, retry_of_job_id)
            if (
                source_job is None
                or source_job.project_id != project_id
                or source_job.job_type != "figures.redraw"
            ):
                raise WorkflowValidationError(
                    "Retry source does not belong to this project's figure workflow."
                )
            source_ids = {
                str(item) for item in source_job.payload.get("figure_ids") or []
            }
            if not set(requested).issubset(source_ids):
                raise WorkflowValidationError(
                    "Retry figures were not part of the source redraw job."
                )
            if source_job.status == "succeeded":
                failed_ids = {
                    str(row.get("figure_id") or "")
                    for row in (source_job.result or {}).get("errors") or []
                    if isinstance(row, dict)
                }
                if not set(requested).issubset(failed_ids):
                    raise WorkflowValidationError(
                        "Only failed items from a partially successful redraw can be retried."
                    )
            elif source_job.status not in {"failed", "interrupted", "cancelled"}:
                raise WorkflowConflict(
                    "Only finished failed, interrupted, or cancelled redraws can be retried."
                )
        current_manifest, _manifest_artifact = self._current_manifest(
            principal, project_id, inputs_artifact.id
        )
        excluded_ids = {
            str(value)
            for value in current_manifest.get("excluded_figure_ids") or []
            if str(value)
        }
        if set(requested) & excluded_ids:
            raise WorkflowConflict(
                "One or more requested figures were explicitly excluded from the manuscript. Re-include them before redrawing.",
                details={"figure_ids": sorted(set(requested) & excluded_ids)},
            )
        baseline_outputs = {
            str(row.get("figure_id") or ""): str(row.get("output_artifact_id") or "")
            for row in current_manifest.get("figures") or []
            if isinstance(row, dict) and row.get("figure_id")
        }
        return {
            "project_id": project_id,
            "source_inputs_artifact_id": inputs_artifact.id,
            "figure_ids": requested,
            "figure_type": str(figure_type or "auto"),
            "origin": "single" if origin == "single" else "batch",
            "baseline_output_artifact_ids": baseline_outputs,
        }

    def resolve_redraw_item(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        figure_id: str,
    ) -> dict[str, Any]:
        figures, inputs_artifact = self._selected_inputs(principal, project_id)
        if inputs_artifact.id != job_payload.get("source_inputs_artifact_id"):
            raise WorkflowConflict(
                "Source figure selections changed while redraw was queued. Submit it again."
            )
        figure = next(
            (row for row in figures if str(row.get("figure_id") or "") == figure_id),
            None,
        )
        if figure is None:
            raise WorkflowNotFound("Selected figure was not found.")
        _source, source_path = self._validate_candidate(
            principal, project_id, figure
        )
        return {
            **job_payload,
            "figure": figure,
            "source_path": str(source_path),
        }

    def _current_manifest(
        self,
        principal: Principal,
        project_id: str,
        inputs_artifact_id: str,
    ) -> tuple[dict[str, Any], ArtifactRecord | None]:
        manifest, artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        if not isinstance(manifest, dict) or manifest.get(
            "source_inputs_artifact_id"
        ) != inputs_artifact_id:
            return {
                "project_id": project_id,
                "source_inputs_artifact_id": inputs_artifact_id,
                "figures": [],
            }, None
        return deepcopy(manifest), artifact

    def publish_redraw(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        figure_id = str(built.get("figure_id") or "")
        if SAFETY_ERROR.search(str(built.get("error") or "")):
            raise FigureSafetyBlocked(
                "The image provider blocked this chemistry figure during safety review. No output was admitted."
            )
        item = self.resolve_redraw_item(
            principal, project_id, job_payload, figure_id
        )
        source_artifact, source_path = self._artifact_path(
            principal,
            project_id,
            str(item["figure"].get("source_image_artifact_id") or ""),
        )
        raw_output = str(built.get("output_path") or "").strip()
        output = Path(raw_output).resolve() if raw_output else None
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        try:
            if output is None:
                raise ValueError
            output.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Redraw worker output escaped its user workspace."
            ) from exc
        if output.is_symlink() or not output.is_file():
            raise FigureOutputUnavailable("The redraw worker produced no usable image.")
        try:
            source_size = image_size(source_path)
            output_size = image_size(output)
        except (OSError, UnidentifiedImageError) as exc:
            raise FigureOutputUnavailable("The redraw worker output is unreadable.") from exc
        with self._write_lock:
            figures, inputs_artifact = self._selected_inputs(principal, project_id)
            if inputs_artifact.id != job_payload.get("source_inputs_artifact_id"):
                raise WorkflowConflict(
                    "Source figure selections changed while redraw was running."
                )
            state = self.repository.get_stage_state(
                principal.user_id, project_id, "figures"
            )
            revision = state.revision if state else 0
            manifest, _manifest_artifact = self._current_manifest(
                principal, project_id, inputs_artifact.id
            )
            run = self.repository.create_stage_run(
                principal.user_id,
                project_id,
                "figures",
                status="succeeded",
                input_snapshot={
                    "source_inputs_artifact_id": inputs_artifact.id,
                    "figure_id": figure_id,
                },
            )
            staging = self.artifacts.stage_run_directory(
                principal.user_id, project_id, run.id
            )
            suffix = output.suffix.casefold() if output.suffix else ".png"
            staged_output = staging / f"redrawn{suffix}"
            shutil.copy2(output, staged_output)
            output_artifact = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                staged_output.name,
                logical_name=f"figures/redrawn/{figure_id}{suffix}",
                artifact_type=suffix.lstrip("."),
                producer_stage="figures",
                make_current=False,
                metadata={"source_artifact_id": source_artifact.id},
            )
            row = {
                key: value
                for key, value in built.items()
                if key not in {"output_path", "error"}
            }
            chemistry = row.get("chemistry_integrity")
            chemistry_status = (
                str(chemistry.get("status") or "")
                if isinstance(chemistry, dict)
                else ""
            )
            if chemistry_status not in {
                "pass",
                "needs_human_arrow_check",
                "failed",
            }:
                row["chemistry_integrity"] = {
                    "status": "failed",
                    "failures": [
                        "The redraw provider did not return a recognized structured chemistry-integrity result."
                    ],
                }
                row["requires_human_chemistry_approval"] = True
                row["output_disposition"] = "saved_with_integrity_warning"
            row.update(
                {
                    "figure_id": figure_id,
                    "paper_id": item["figure"].get("paper_id"),
                    "section_id": item["figure"].get("section_id"),
                    "section_heading": item["figure"].get("section_heading"),
                    "target_paragraph_id": item["figure"].get("target_paragraph_id"),
                    "source_label": item["figure"].get("source_label"),
                    "source_caption_text": item["figure"].get("source_caption_text"),
                    "representative_role": item["figure"].get("representative_role"),
                    "source_artifact_id": source_artifact.id,
                    "output_artifact_id": output_artifact.id,
                    "producer_job_id": str(job_payload.get("producer_job_id") or ""),
                    "status": "redrawn",
                    "aspect_ratio_integrity": aspect_ratio_integrity(
                        source_size, output_size
                    ),
                    "updated_at": utc_now().isoformat(),
                }
            )
            row.update(
                publication_caption_fields(
                    item["figure"].get("source_caption_text"),
                    representative_role=item["figure"].get("representative_role"),
                    source_label=item["figure"].get("source_label"),
                    context_title=item["figure"].get("section_heading"),
                )
            )
            rows = [
                old
                for old in manifest.get("figures") or []
                if isinstance(old, dict)
                and str(old.get("figure_id") or "") != figure_id
            ]
            rows.append(row)
            manifest["figures"] = rows
            manifest["updated_at"] = utc_now().isoformat()
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_artifact = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                manifest_path.name,
                logical_name=FIGURE_MANIFEST,
                artifact_type="json",
                producer_stage="figures",
                make_current=False,
            )
            promoted = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "figures",
                artifact_ids={
                    output_artifact.logical_name: output_artifact.id,
                    FIGURE_MANIFEST: manifest_artifact.id,
                },
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=("draft", "final"),
            )
        return {
            "figure_id": figure_id,
            "output_artifact_id": output_artifact.id,
            "manifest_artifact_id": manifest_artifact.id,
            "revision": promoted.revision,
        }

    def _public_manifest(
        self,
        principal: Principal,
        project_id: str,
        figures: list[dict[str, Any]],
        inputs_artifact: ArtifactRecord,
    ) -> tuple[dict[str, Any] | None, set[str]]:
        manifest, manifest_artifact = self._current_manifest(
            principal, project_id, inputs_artifact.id
        )
        if manifest_artifact is None:
            return None, set()
        selected = {str(row.get("figure_id") or ""): row for row in figures}
        excluded_ids = {
            str(value)
            for value in manifest.get("excluded_figure_ids") or []
            if str(value)
        }
        public_rows: list[dict[str, Any]] = []
        usable: set[str] = set()
        for raw_row in manifest.get("figures") or []:
            if not isinstance(raw_row, dict):
                continue
            row = deepcopy(raw_row)
            figure_id = str(row.get("figure_id") or "")
            figure = selected.get(figure_id)
            if not figure:
                continue
            current_source_id = str(figure.get("source_image_artifact_id") or "")
            source_current = row.get("source_artifact_id") == current_source_id
            output_id = str(row.get("output_artifact_id") or "")
            try:
                _source_artifact, source_path = self._artifact_path(
                    principal, project_id, current_source_id
                )
                _output_artifact, output_path = self._artifact_path(
                    principal, project_id, output_id
                )
                integrity = aspect_ratio_integrity(
                    image_size(source_path), image_size(output_path)
                )
                edge_check = _edge_check(output_path)
                output_current = True
            except (
                WorkflowNotFound,
                WorkflowValidationError,
                OSError,
                UnidentifiedImageError,
            ):
                integrity = {"status": "unavailable"}
                edge_check = {"status": "unavailable", "ink_touches_edge": None}
                output_current = False
            row["aspect_ratio_integrity"] = integrity
            row["edge_check"] = edge_check
            approval = dict(row.get("human_approval") or {})
            approval["current_source_match"] = bool(
                approval.get("source_artifact_id") == current_source_id
            )
            approval["current_output_match"] = bool(
                approval.get("output_artifact_id") == output_id and output_current
            )
            if approval:
                row["human_approval"] = approval
            chemistry = str(
                (row.get("chemistry_integrity") or {}).get("status") or "missing"
            )
            requires_approval = bool(
                chemistry != "pass"
                or row.get("render_mode") in {"manual-svg", "manual-arrow-edit"}
                or row.get("requires_human_chemistry_approval")
                or row.get("output_disposition") == "saved_with_integrity_warning"
            )
            approved = bool(
                approval.get("status") == "approved"
                and approval.get("current_source_match")
                and approval.get("current_output_match")
            )
            policy_ok = bool(
                canvas_policy_matches(row, integrity)
                or (approved and approval.get("manual_canvas_override"))
            )
            if (
                source_current
                and output_current
                and row.get("status") == "redrawn"
                and policy_ok
                and (not requires_approval or approved)
            ):
                usable.add(figure_id)
            row["source_current"] = source_current
            row["output_current"] = output_current
            row["requires_human_approval"] = requires_approval
            row["manuscript_selected"] = figure_id not in excluded_ids
            if figure_id in excluded_ids:
                usable.discard(figure_id)
            row["usable"] = figure_id in usable
            if output_id:
                row["redrawn_image_url"] = artifact_url(output_id)
                row["redrawn_image"] = row["redrawn_image_url"]
            svg_id = str(row.get("editable_svg_artifact_id") or "")
            if svg_id:
                row["editable_svg_url"] = artifact_url(svg_id)
                row["editable_svg"] = row["editable_svg_url"]
            audit_id = str(row.get("audit_artifact_id") or "")
            if audit_id:
                row["audit_url"] = artifact_url(audit_id)
                row.setdefault("manual_edit", {})["audit_path"] = row["audit_url"]
            if isinstance(row.get("manual_edit"), dict):
                row["manual_arrow_edit"] = deepcopy(row["manual_edit"])
                if svg_id:
                    row["manual_arrow_edit"]["editable_svg"] = artifact_url(svg_id)
                if audit_id:
                    row["manual_arrow_edit"]["audit_path"] = artifact_url(audit_id)
                row["manual_canvas_review_eligible"] = bool(
                    row.get("render_mode") == "manual-arrow-edit"
                    and integrity.get("status") == "failed"
                    and (row["manual_arrow_edit"].get("canvas_crop") or {}).get("status")
                    == "verified"
                )
            public_rows.append(row)
        return {
            **manifest,
            "artifact_id": manifest_artifact.id,
            "figures": public_rows,
        }, usable

    @staticmethod
    def _job_payload(job) -> dict[str, Any]:
        return {
            "id": job.id,
            "status": job.status,
            "figure_ids": list(job.payload.get("figure_ids") or []),
            "progress_current": job.progress_current,
            "progress_total": job.progress_total,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "result": job.result,
            "retry_of_job_id": job.retry_of_job_id,
        }

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        figures, inputs_artifact = self._selected_inputs(principal, project_id)
        current_manifest, _current_manifest_artifact = self._current_manifest(
            principal, project_id, inputs_artifact.id
        )
        excluded_ids = {
            str(value)
            for value in current_manifest.get("excluded_figure_ids") or []
            if str(value)
        }
        public_figures: list[dict[str, Any]] = []
        for raw in figures:
            row = deepcopy(raw)
            row["manuscript_selected"] = (
                str(row.get("figure_id") or "") not in excluded_ids
            )
            source_id = str(row.get("source_image_artifact_id") or "")
            row["source_image_url"] = artifact_url(source_id)
            row["source_image_path"] = row["source_image_url"]
            public_figures.append(row)
        manifest, usable = self._public_manifest(
            principal, project_id, figures, inputs_artifact
        )
        current_outputs = {
            str(row.get("figure_id") or ""): str(row.get("output_artifact_id") or "")
            for row in (manifest or {}).get("figures") or []
            if isinstance(row, dict) and row.get("figure_id")
        }
        current_producers = {
            str(row.get("figure_id") or ""): str(row.get("producer_job_id") or "")
            for row in (manifest or {}).get("figures") or []
            if isinstance(row, dict) and row.get("figure_id")
        }
        jobs = self.repository.list_project_jobs(
            principal.user_id, project_id, job_type="figures.redraw"
        )
        batch_jobs = [job for job in jobs if job.payload.get("origin") == "batch"]
        active = next(
            (
                job
                for job in batch_jobs
                if job.status in {"queued", "running", "cancel_requested"}
            ),
            None,
        )
        states: dict[str, dict[str, Any]] = {}
        for job in jobs:
            result = job.result if isinstance(job.result, dict) else {}
            output_ids = {
                str(row.get("figure_id") or "")
                for row in result.get("outputs") or []
                if isinstance(row, dict) and row.get("figure_id")
            }
            baseline = {
                str(key): str(value or "")
                for key, value in (
                    job.payload.get("baseline_output_artifact_ids") or {}
                ).items()
            }
            output_ids.update(
                figure_id
                for figure_id in job.payload.get("figure_ids") or []
                if current_outputs.get(str(figure_id))
                and current_outputs.get(str(figure_id))
                != baseline.get(str(figure_id), "")
                and current_producers.get(str(figure_id)) == job.id
            )
            item_errors = {
                str(row.get("figure_id") or ""): row
                for row in result.get("errors") or []
                if isinstance(row, dict) and row.get("figure_id")
            }
            for figure_id in job.payload.get("figure_ids") or []:
                figure_id = str(figure_id)
                item_error = item_errors.get(figure_id)
                display_status = job.status
                display_error = job.error_message
                if figure_id in output_ids:
                    display_status = "completed"
                    display_error = ""
                elif item_error is not None:
                    display_status = "failed"
                    display_error = str(item_error.get("error") or "")
                states.setdefault(
                    figure_id,
                    {
                        "status": display_status,
                        "job_id": job.id,
                        "job_status": job.status,
                        "origin": str(job.payload.get("origin") or ""),
                        "retry_of_job_id": job.retry_of_job_id,
                        "error": display_error,
                    },
                )
        display_job = active or (batch_jobs[0] if batch_jobs else None)
        display_result = (
            display_job.result
            if display_job is not None and isinstance(display_job.result, dict)
            else {}
        )
        result_outputs = [
            row for row in display_result.get("outputs") or [] if isinstance(row, dict)
        ]
        if display_job is not None:
            baseline = {
                str(key): str(value or "")
                for key, value in (
                    display_job.payload.get("baseline_output_artifact_ids") or {}
                ).items()
            }
            recorded_output_ids = {
                str(row.get("figure_id") or "")
                for row in result_outputs
                if row.get("figure_id")
            }
            for figure_id in display_job.payload.get("figure_ids") or []:
                figure_id = str(figure_id)
                if (
                    figure_id not in recorded_output_ids
                    and current_outputs.get(figure_id)
                    and current_outputs.get(figure_id) != baseline.get(figure_id, "")
                    and current_producers.get(figure_id) == display_job.id
                ):
                    result_outputs.append({"figure_id": figure_id, "recovered": True})
        result_errors = [
            row for row in display_result.get("errors") or [] if isinstance(row, dict)
        ]
        if display_job is not None and display_job.status == "failed" and not result_errors:
            result_errors = [
                {"job_id": display_job.id, "error": display_job.error_message}
            ]
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "figures"
        )
        selected_ids = {
            str(row.get("figure_id") or "")
            for row in figures
            if row.get("figure_id")
            and row.get("manuscript_selected") is not False
            and str(row.get("figure_id") or "") not in excluded_ids
        }
        return {
            "project_id": project_id,
            "figure_candidates": public_figures,
            "paper_display_labels": self._paper_display_labels(
                principal,
                project_id,
                [str(row.get("paper_id") or "") for row in public_figures],
            ),
            "redrawn_manifest": manifest,
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "batch_redraw": {
                "job_id": display_job.id if display_job else "",
                "status": display_job.status if display_job else "idle",
                "total": display_job.progress_total if display_job else 0,
                "completed": display_job.progress_current if display_job else 0,
                "succeeded": len(result_outputs),
                "failed": len(result_errors),
                "current_figure_id": "",
                "errors": result_errors,
            },
            "figure_redraw_states": states,
            "figure_type_options": [
                {"value": "auto", "label": "Automatic"},
                {"value": "mechanism-cycle", "label": "Mechanism"},
                {"value": "simple-scheme", "label": "Simple reaction scheme"},
                {"value": "reaction-scope", "label": "Reaction scope"},
                {"value": "complex-multipanel", "label": "Complex multi-panel chemistry"},
                {"value": "low-resolution", "label": "Low-resolution / thin-line chemistry"},
                {"value": "colored-chemistry", "label": "Colored chemistry / remove decorative fills"},
                {"value": "data-table", "label": "Data table"},
                {"value": "scientific-plot", "label": "Scientific plot"},
                {"value": "general-scientific", "label": "Overview"},
            ],
            "freshness": {
                "source_stale": False,
                "redraw_stale": bool(selected_ids - usable),
                "semantic_redraw_stale": bool(selected_ids - usable),
                "selected_count": len(selected_ids),
                "usable_count": len(selected_ids & usable),
                "stale": bool(selected_ids - usable),
            },
            "report": {"jobs": [self._job_payload(job) for job in jobs]},
        }

    def set_manuscript_inclusion(
        self,
        principal: Principal,
        project_id: str,
        figure_id: str,
        *,
        included: bool,
    ) -> dict[str, Any]:
        """Include/exclude one figure without changing its Stage 4 source selection."""

        principal.require(Permission.PROJECT_WRITE)
        figures, inputs_artifact = self._selected_inputs(principal, project_id)
        selected_ids = {
            str(row.get("figure_id") or "")
            for row in figures
            if str(row.get("figure_id") or "")
        }
        normalized = str(figure_id or "").strip()
        if normalized not in selected_ids:
            raise WorkflowNotFound("Selected figure was not found.")
        manifest, manifest_artifact = self._current_manifest(
            principal, project_id, inputs_artifact.id
        )
        excluded = {
            str(value)
            for value in manifest.get("excluded_figure_ids") or []
            if str(value)
        }
        if included:
            excluded.discard(normalized)
        else:
            excluded.add(normalized)
        manifest["excluded_figure_ids"] = sorted(excluded)
        manifest["updated_at"] = utc_now().isoformat()
        stage_state = self.repository.get_stage_state(
            principal.user_id, project_id, "figures"
        )
        published, state = self._publish_files(
            principal,
            project_id,
            stage_id="figures",
            files={
                FIGURE_MANIFEST: (
                    (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                    "json",
                )
            },
            expected_revision=stage_state.revision if stage_state else 0,
            status="review",
            invalidate_stages=("draft", "final"),
            metadata={
                "figure_id": normalized,
                "manuscript_selected": included,
            },
            expected_current_artifacts=(
                {FIGURE_MANIFEST: manifest_artifact.id}
                if manifest_artifact is not None
                else None
            ),
        )
        return {
            "figure_id": normalized,
            "manuscript_selected": included,
            "manifest_artifact_id": published[FIGURE_MANIFEST].id,
            "revision": state.revision,
        }

    def create_full_svg(
        self,
        principal: Principal,
        project_id: str,
        figure_id: str,
        *,
        base_mode: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        figures, _inputs = self._selected_inputs(principal, project_id)
        figure = next(
            (row for row in figures if str(row.get("figure_id") or "") == figure_id),
            None,
        )
        if figure is None:
            raise WorkflowNotFound("Confirmed figure was not found.")
        _source, base_path = self._validate_candidate(principal, project_id, figure)
        redrawn: dict[str, Any] | None = None
        if base_mode == "redrawn":
            payload = self.get(principal, project_id)
            manifest_rows = (payload.get("redrawn_manifest") or {}).get("figures") or []
            redrawn = next(
                (row for row in manifest_rows if row.get("figure_id") == figure_id),
                None,
            )
            if not redrawn or not redrawn.get("output_artifact_id"):
                raise FigureOutputUnavailable(
                    "The selected AI redraw is unavailable. Use the source image or redraw it."
                )
            _output, base_path = self._artifact_path(
                principal, project_id, redrawn["output_artifact_id"]
            )
        reused_saved_workspace = False
        width, height = image_size(base_path)
        svg = ""
        if base_mode == "redrawn" and redrawn:
            saved_svg_id = str(redrawn.get("editable_svg_artifact_id") or "")
            if saved_svg_id:
                try:
                    _saved_svg_artifact, saved_svg_path = self._artifact_path(
                        principal, project_id, saved_svg_id
                    )
                    saved_svg = saved_svg_path.read_text(encoding="utf-8")
                    validate_svg_markup(saved_svg, require_full_trace=True)
                    width, height = svg_workspace_size(saved_svg)
                    svg = saved_svg
                    reused_saved_workspace = True
                except (OSError, UnicodeError, ValueError, WorkflowNotFound):
                    svg = ""
        if not svg:
            svg = build_full_vector_svg(base_path)
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            "figures",
            status="succeeded",
            input_snapshot={"figure_id": figure_id, "base_mode": base_mode},
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        path = staging / f"{figure_id}-editor.svg"
        path.write_text(svg, encoding="utf-8")
        artifact = self.artifacts.publish(
            principal.user_id,
            project_id,
            run.id,
            path.name,
            logical_name=f"figures/editor-workspaces/{figure_id}.svg",
            artifact_type="svg",
            producer_stage="figures",
            make_current=False,
            metadata={"base_mode": base_mode},
        )
        return {
            "figure_id": figure_id,
            "base_mode": base_mode,
            "base_width": width,
            "base_height": height,
            "full_svg_artifact_id": artifact.id,
            "full_svg_url": artifact_url(artifact.id),
            "full_svg": artifact_url(artifact.id),
            "contains_embedded_raster": False,
            "reused_saved_workspace": reused_saved_workspace,
        }

    @staticmethod
    def _decode_png(data_url: str) -> bytes:
        match = PNG_DATA_URL.fullmatch(str(data_url or ""))
        if not match:
            raise WorkflowValidationError(
                "image_png_data_url must be a PNG data URL."
            )
        try:
            content = base64.b64decode(match.group(1), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkflowValidationError("Manual edit PNG is not valid base64.") from exc
        try:
            png_size(content)
        except ValueError as exc:
            raise WorkflowValidationError(str(exc)) from exc
        return content

    def save_manual_edit(
        self,
        principal: Principal,
        project_id: str,
        figure_id: str,
        *,
        image_png_data_url: str,
        operations: list[dict[str, Any]],
        base_mode: str,
        editable_svg: str,
        full_vector_svg: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        png = self._decode_png(image_png_data_url)
        svg = str(full_vector_svg or "")
        if svg:
            try:
                validate_svg_markup(svg, require_full_trace=True)
            except ValueError as exc:
                raise WorkflowValidationError(str(exc)) from exc
        figures, inputs_artifact = self._selected_inputs(principal, project_id)
        figure = next(
            (row for row in figures if str(row.get("figure_id") or "") == figure_id),
            None,
        )
        if figure is None:
            raise WorkflowNotFound("Confirmed figure was not found.")
        source_artifact, source_path = self._artifact_path(
            principal,
            project_id,
            str(figure.get("source_image_artifact_id") or ""),
        )
        base_path = source_path
        existing_workspace_size: tuple[int, int] | None = None
        if base_mode == "redrawn":
            current = self.get(principal, project_id)
            current_rows = (current.get("redrawn_manifest") or {}).get("figures") or []
            current_row = next(
                (row for row in current_rows if row.get("figure_id") == figure_id),
                None,
            )
            if not current_row or not current_row.get("output_artifact_id"):
                raise FigureOutputUnavailable("The selected AI redraw is unavailable.")
            _base_artifact, base_path = self._artifact_path(
                principal, project_id, current_row["output_artifact_id"]
            )
            saved_svg_id = str(current_row.get("editable_svg_artifact_id") or "")
            if saved_svg_id:
                try:
                    _saved_svg_artifact, saved_svg_path = self._artifact_path(
                        principal, project_id, saved_svg_id
                    )
                    saved_svg = saved_svg_path.read_text(encoding="utf-8")
                    validate_svg_markup(saved_svg, require_full_trace=True)
                    existing_workspace_size = svg_workspace_size(saved_svg)
                except (OSError, UnicodeError, ValueError, WorkflowNotFound):
                    existing_workspace_size = None
        if not svg:
            try:
                svg = append_operation_overlays(
                    build_full_vector_svg(base_path), operations
                )
            except ValueError as exc:
                raise WorkflowValidationError(str(exc)) from exc
        base_size = existing_workspace_size or image_size(base_path)
        submitted_size = png_size(png)
        try:
            crop = validated_content_crop(svg, base_size, submitted_size)
        except ValueError as exc:
            raise FigureCanvasMismatch(str(exc)) from exc
        if submitted_size != base_size and crop is None:
            raise FigureCanvasMismatch(
                f"Canvas size {submitted_size} does not match selected base image {base_size}. Use Crop Canvas and save the verified crop metadata."
            )
        source_size = image_size(source_path)
        integrity = aspect_ratio_integrity(source_size, submitted_size)
        audit = {
            "figure_id": figure_id,
            "source_artifact_id": source_artifact.id,
            "base_mode": base_mode,
            "submitted_canvas_size": list(submitted_size),
            "base_canvas_size": list(base_size),
            "output_canvas_size": list(submitted_size),
            "canvas_crop": crop,
            "operations": operations,
            "saved_at": utc_now().isoformat(),
        }
        with self._write_lock:
            state = self.repository.get_stage_state(
                principal.user_id, project_id, "figures"
            )
            revision = state.revision if state else 0
            manifest, _artifact = self._current_manifest(
                principal, project_id, inputs_artifact.id
            )
            run = self.repository.create_stage_run(
                principal.user_id,
                project_id,
                "figures",
                status="succeeded",
                input_snapshot={"figure_id": figure_id, "base_mode": base_mode},
            )
            staging = self.artifacts.stage_run_directory(
                principal.user_id, project_id, run.id
            )
            (staging / "manual.png").write_bytes(png)
            (staging / "manual.svg").write_text(svg, encoding="utf-8")
            (staging / "audit.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output_artifact = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                "manual.png",
                logical_name=f"figures/redrawn/{figure_id}.png",
                artifact_type="png",
                producer_stage="figures",
                make_current=False,
                metadata={"source_artifact_id": source_artifact.id, "manual": True},
            )
            svg_artifact = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                "manual.svg",
                logical_name=f"figures/manual-edits/{figure_id}.svg",
                artifact_type="svg",
                producer_stage="figures",
                make_current=False,
                metadata={"source_artifact_id": source_artifact.id},
            )
            audit["output_artifact_id"] = output_artifact.id
            audit["editable_svg_artifact_id"] = svg_artifact.id
            (staging / "audit-published.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            audit_artifact = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                "audit-published.json",
                logical_name=f"figures/manual-edits/{figure_id}.json",
                artifact_type="json",
                producer_stage="figures",
                make_current=False,
            )
            row = {
                **figure,
                **publication_caption_fields(
                    figure.get("source_caption_text"),
                    representative_role=figure.get("representative_role"),
                    source_label=figure.get("source_label"),
                    context_title=figure.get("section_heading"),
                ),
                "figure_id": figure_id,
                "source_artifact_id": source_artifact.id,
                "output_artifact_id": output_artifact.id,
                "editable_svg_artifact_id": svg_artifact.id,
                "audit_artifact_id": audit_artifact.id,
                "render_mode": "manual-arrow-edit",
                "status": "redrawn",
                "aspect_ratio_integrity": integrity,
                "aspect_ratio_policy": (
                    "content_crop_allowed" if crop else "source_ratio_required"
                ),
                "manual_edit": {
                    "status": "saved",
                    "base_mode": base_mode,
                    "canvas_crop": crop,
                    "operations_count": len(operations),
                    "full_image_vector_trace": True,
                },
                "chemistry_integrity": {
                    "status": "needs_human_arrow_check",
                    "failures": [
                        "Manual SVG changes require human verification of structures, labels, bonds, and arrows."
                    ],
                },
                "requires_human_chemistry_approval": True,
                "updated_at": utc_now().isoformat(),
            }
            rows = [
                old
                for old in manifest.get("figures") or []
                if isinstance(old, dict)
                and str(old.get("figure_id") or "") != figure_id
            ]
            rows.append(row)
            manifest["figures"] = rows
            manifest["updated_at"] = utc_now().isoformat()
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_artifact = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                "manifest.json",
                logical_name=FIGURE_MANIFEST,
                artifact_type="json",
                producer_stage="figures",
                make_current=False,
            )
            promoted = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "figures",
                artifact_ids={
                    output_artifact.logical_name: output_artifact.id,
                    svg_artifact.logical_name: svg_artifact.id,
                    audit_artifact.logical_name: audit_artifact.id,
                    FIGURE_MANIFEST: manifest_artifact.id,
                },
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=("draft", "final"),
            )
        return {
            "figure_id": figure_id,
            "output_artifact_id": output_artifact.id,
            "editable_svg_artifact_id": svg_artifact.id,
            "audit_artifact_id": audit_artifact.id,
            "revision": promoted.revision,
        }

    def _approve_rows(
        self,
        principal: Principal,
        project_id: str,
        figure_ids: set[str],
        *,
        allow_canvas_override: bool,
    ) -> tuple[
        dict[str, Any],
        list[str],
        list[str],
        list[str],
        list[dict[str, Any]],
        int,
        str,
    ]:
        figures, inputs_artifact = self._selected_inputs(principal, project_id)
        selected = {str(row.get("figure_id") or ""): row for row in figures}
        stage_state = self.repository.get_stage_state(
            principal.user_id, project_id, "figures"
        )
        expected_revision = stage_state.revision if stage_state else 0
        manifest, manifest_artifact = self._current_manifest(
            principal, project_id, inputs_artifact.id
        )
        if manifest_artifact is None:
            raise FigureOutputUnavailable("No redraw or manual figure output is available.")
        approved: list[str] = []
        already_approved: list[str] = []
        skipped: list[str] = []
        visited: set[str] = set()
        approval_events: list[dict[str, Any]] = []
        for row in manifest.get("figures") or []:
            if not isinstance(row, dict):
                continue
            figure_id = str(row.get("figure_id") or "")
            if figure_id not in figure_ids:
                continue
            visited.add(figure_id)
            figure = selected.get(figure_id)
            if not figure or row.get("status") != "redrawn":
                skipped.append(figure_id)
                continue
            current_source_id = str(figure.get("source_image_artifact_id") or "")
            if row.get("source_artifact_id") != current_source_id:
                skipped.append(figure_id)
                continue
            output_id = str(row.get("output_artifact_id") or "")
            current_approval = row.get("human_approval") or {}
            if (
                current_approval.get("status") == "approved"
                and current_approval.get("source_artifact_id") == current_source_id
                and current_approval.get("output_artifact_id") == output_id
            ):
                already_approved.append(figure_id)
                continue
            _source_artifact, source_path = self._artifact_path(
                principal, project_id, current_source_id
            )
            _output_artifact, output_path = self._artifact_path(
                principal, project_id, output_id
            )
            integrity = aspect_ratio_integrity(
                image_size(source_path), image_size(output_path)
            )
            crop = (row.get("manual_edit") or {}).get("canvas_crop") or {}
            verified_crop_override = bool(
                row.get("render_mode") == "manual-arrow-edit"
                and integrity.get("status") == "failed"
                and crop.get("status") == "verified"
            )
            canvas_warning = integrity.get("status") != "pass"
            manual_override = bool(
                verified_crop_override or (allow_canvas_override and canvas_warning)
            )
            if integrity.get("status") != "pass" and not manual_override:
                skipped.append(figure_id)
                continue
            approval_id = str(uuid.uuid4())
            approval_time = utc_now()
            approval_details = {
                "source_artifact_id": current_source_id,
                "output_artifact_id": output_id,
                "manual_canvas_override": manual_override,
                "source_canvas_size": integrity.get("source_size"),
                "output_canvas_size": integrity.get("output_size"),
            }
            approval_events.append(
                {
                    "id": approval_id,
                    "stage_id": "figures",
                    "subject_type": "figure-output",
                    "subject_id": figure_id,
                    "decision": "approved",
                    "details": approval_details,
                    "created_at": approval_time,
                }
            )
            row["aspect_ratio_integrity"] = integrity
            row["human_approval"] = {
                "id": approval_id,
                "status": "approved",
                "approved_at": approval_time.isoformat(),
                "source_artifact_id": current_source_id,
                "output_artifact_id": output_id,
                "manual_canvas_override": manual_override,
                "source_canvas_size": integrity.get("source_size"),
                "output_canvas_size": integrity.get("output_size"),
                "acknowledgement": (
                    "A human reviewer inspected chemical structures, bonds, labels, arrows, process, and layout."
                ),
            }
            row["output_disposition"] = "human_approved_for_manuscript"
            approved.append(figure_id)
        skipped.extend(sorted(figure_ids - visited))
        return (
            manifest,
            approved,
            already_approved,
            skipped,
            approval_events,
            expected_revision,
            manifest_artifact.id,
        )

    def approve(
        self,
        principal: Principal,
        project_id: str,
        figure_id: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        with self._write_lock:
            (
                manifest,
                approved,
                already_approved,
                skipped,
                approval_events,
                expected_revision,
                expected_manifest_id,
            ) = self._approve_rows(
                principal,
                project_id,
                {figure_id},
                allow_canvas_override=True,
            )
            if figure_id in already_approved:
                state = self.repository.get_stage_state(
                    principal.user_id, project_id, "figures"
                )
                return {
                    "figure_id": figure_id,
                    "approved": True,
                    "already_approved": True,
                    "revision": state.revision if state else 0,
                }
            if figure_id not in approved:
                raise FigureOutputUnavailable(
                    "The current figure output cannot be approved."
                )
            published, next_state = self._publish_files(
                principal,
                project_id,
                stage_id="figures",
                files={
                    FIGURE_MANIFEST: (
                        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=expected_revision,
                status="review",
                invalidate_stages=("draft", "final"),
                metadata={"approval_figure_ids": approved},
                approval_events=approval_events,
                expected_current_artifacts={FIGURE_MANIFEST: expected_manifest_id},
            )
        return {
            "figure_id": figure_id,
            "approved": True,
            "manifest_artifact_id": published[FIGURE_MANIFEST].id,
            "revision": next_state.revision,
        }

    def approve_successful(
        self, principal: Principal, project_id: str
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        figures, _inputs = self._selected_inputs(principal, project_id)
        all_ids = {str(row.get("figure_id") or "") for row in figures}
        with self._write_lock:
            (
                manifest,
                approved,
                already_approved,
                skipped,
                approval_events,
                expected_revision,
                expected_manifest_id,
            ) = self._approve_rows(
                principal,
                project_id,
                all_ids,
                allow_canvas_override=True,
            )
            if not approved:
                return {
                    "approved_count": 0,
                    "already_approved_count": len(already_approved),
                    "skipped_count": len(skipped),
                    "generation_failed_count": len(skipped),
                }
            self._publish_files(
                principal,
                project_id,
                stage_id="figures",
                files={
                    FIGURE_MANIFEST: (
                        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=expected_revision,
                status="review",
                invalidate_stages=("draft", "final"),
                metadata={"approval_figure_ids": approved},
                approval_events=approval_events,
                expected_current_artifacts={FIGURE_MANIFEST: expected_manifest_id},
            )
        return {
            "approved_count": len(approved),
            "already_approved_count": len(already_approved),
            "skipped_count": len(skipped),
            "generation_failed_count": len(skipped),
        }

    def confirm(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        payload = self.get(principal, project_id)
        selected = int(payload["freshness"]["selected_count"])
        usable = int(payload["freshness"]["usable_count"])
        active = payload.get("batch_redraw") or {}
        if active.get("status") in {"queued", "running", "cancel_requested"}:
            raise FigureOutputsIncomplete(
                "Figure generation is still running. Wait or stop it before entering Draft."
            )
        unplaced = [
            str(row.get("figure_id") or "")
            for row in payload.get("figure_candidates") or []
            if isinstance(row, dict)
            and row.get("manuscript_selected") is not False
            and not str(row.get("target_paragraph_id") or "").strip()
        ]
        if unplaced:
            raise FigureOutputsIncomplete(
                "One or more paper-level figures have no supported paragraph placement in the current section drafts. Exclude them or regenerate the relevant section before entering Draft.",
                details={"figure_ids": unplaced, "reason": "waiting_for_supported_paragraph"},
            )
        if selected <= 0 or usable != selected:
            raise FigureOutputsIncomplete(
                f"Figure redraw is incomplete or out of date ({usable}/{selected} current outputs are usable). Redraw missing figures or approve warning outputs before building the draft.",
                details={
                    "selected_count": selected,
                    "usable_count": usable,
                    "remaining_count": max(0, selected - usable),
                },
            )
        state = self.repository.compare_and_set_stage(
            principal.user_id,
            project_id,
            "figures",
            int(revision),
            status="approved",
        )
        return {
            "project_id": project_id,
            "revision": state.revision,
            "status": state.status,
            "next_stage": "draft",
        }
