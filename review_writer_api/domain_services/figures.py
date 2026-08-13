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

from PIL import UnidentifiedImageError

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
    validate_svg_markup,
    validated_content_crop,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository


SECTION_INDEX = "sections/section_drafts.json"
PAPER_CANDIDATES = "sections/paper_figure_candidates.json"
FIGURE_CANDIDATES = "sections/figure_candidates.json"
DEFAULT_REVIEWS = "sections/default_figure_reviews.json"
REVIEW_SELECTIONS = "figure-review/selections.json"
REVIEW_INPUTS = "figure-review/selected_figures.json"
FIGURE_MANIFEST = "figures/manifest.json"
PNG_DATA_URL = re.compile(r"^data:image/png;base64,(.+)$", re.DOTALL)
SAFETY_ERROR = re.compile(
    r"adult content|sexual content|safety policy|safety review|moderation",
    re.IGNORECASE,
)


def artifact_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{artifact_id}/content"


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


class FigureCanvasApprovalBlocked(WorkflowConflict):
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
        anchor = str(
            candidate.get("target_paragraph_id")
            or candidate.get("paragraph_id")
            or ""
        ).strip()
        if not anchor:
            raise FigureParagraphAnchorMissing(
                "The selected candidate has no matching manuscript paragraph anchor."
            )
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
                bool(candidate.get("source_image_url"))
                and bool(
                    str(
                        candidate.get("target_paragraph_id")
                        or candidate.get("paragraph_id")
                        or ""
                    ).strip()
                )
                for candidate in candidates
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

    def save_review_selection(
        self,
        principal: Principal,
        project_id: str,
        paper_id: str,
        *,
        revision: int,
        candidate_index: int,
        review_note: str,
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
            "selection_source": "human",
            "reviewed_at": utc_now().isoformat(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                stage_id="figure-review",
                files={
                    REVIEW_SELECTIONS: (
                        (json.dumps(reviews, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                status="review",
                invalidate_stages=("figures", "draft", "final"),
                metadata={"source_paper_candidates_artifact_id": paper_artifact.id},
            )
        return {
            "project_id": project_id,
            "paper_id": paper_id,
            "candidate_index": candidate_index,
            "revision": state.revision,
            "status": state.status,
            "selection_artifact_id": published[REVIEW_SELECTIONS].id,
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
                    and bool(
                        str(
                            candidate.get("target_paragraph_id")
                            or candidate.get("paragraph_id")
                            or ""
                        ).strip()
                    )
                    for candidate in paper.get("candidates") or []
                )
                if reviewable:
                    missing.append(paper_id)
                continue
            _paper, candidate = self._candidate(papers, paper_id, selected_index)
            source_artifact, _source_path = self._validate_candidate(
                principal, project_id, candidate
            )
            candidate["source_image_artifact_id"] = source_artifact.id
            candidate["source_review_note"] = str(review.get("review_note") or "")
            selected.append(candidate)
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
                status="approved",
                invalidate_stages=("figures", "draft", "final"),
            )
        return {
            "project_id": project_id,
            "status": state.status,
            "revision": state.revision,
            "selected_count": len(selected),
            "selected_figures_artifact_id": inputs_artifact.id,
            "next_tab": "redraw",
        }

    def _selected_inputs(
        self, principal: Principal, project_id: str
    ) -> tuple[list[dict[str, Any]], ArtifactRecord]:
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "figure-review"
        )
        if state is None or state.status != "approved":
            raise FigureReviewIncomplete(
                "Confirm source figure review before redrawing figures."
            )
        payload, artifact = self._read_json(principal, project_id, REVIEW_INPUTS)
        figures = payload.get("figures") if isinstance(payload, dict) else None
        rows = [dict(row) for row in figures or [] if isinstance(row, dict)]
        if not rows:
            raise FigureReviewIncomplete("No confirmed source figures are available.")
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
            requested = list(available)
        unknown = sorted(set(requested) - set(available))
        if unknown:
            raise WorkflowValidationError(
                "One or more requested figures are not in the confirmed source set.",
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
            raise WorkflowNotFound("Confirmed figure was not found.")
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
                    "source_artifact_id": source_artifact.id,
                    "source_image_sha256": source_artifact.content_sha256,
                    "output_artifact_id": output_artifact.id,
                    "output_image_sha256": output_artifact.content_sha256,
                    "producer_job_id": str(job_payload.get("producer_job_id") or ""),
                    "status": "redrawn",
                    "aspect_ratio_integrity": aspect_ratio_integrity(
                        source_size, output_size
                    ),
                    "updated_at": utc_now().isoformat(),
                }
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
                output_current = True
            except (
                WorkflowNotFound,
                WorkflowValidationError,
                OSError,
                UnidentifiedImageError,
            ):
                integrity = {"status": "unavailable"}
                output_current = False
            row["aspect_ratio_integrity"] = integrity
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
            policy_ok = canvas_policy_matches(row, integrity)
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
        public_figures: list[dict[str, Any]] = []
        for raw in figures:
            row = deepcopy(raw)
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
            if row.get("figure_id") and row.get("manuscript_selected") is not False
        }
        return {
            "project_id": project_id,
            "figure_candidates": public_figures,
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
                {"value": "reaction-scope", "label": "Reaction scope"},
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
        width, height = image_size(base_path)
        return {
            "figure_id": figure_id,
            "base_mode": base_mode,
            "base_width": width,
            "base_height": height,
            "full_svg_artifact_id": artifact.id,
            "full_svg_url": artifact_url(artifact.id),
            "full_svg": artifact_url(artifact.id),
            "contains_embedded_raster": False,
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
        if not svg:
            try:
                svg = append_operation_overlays(
                    build_full_vector_svg(base_path), operations
                )
            except ValueError as exc:
                raise WorkflowValidationError(str(exc)) from exc
        base_size = image_size(base_path)
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
                "figure_id": figure_id,
                "source_artifact_id": source_artifact.id,
                "source_image_sha256": source_artifact.content_sha256,
                "output_artifact_id": output_artifact.id,
                "output_image_sha256": output_artifact.content_sha256,
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
        strict_canvas: bool,
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
            manual_override = bool(
                row.get("render_mode") == "manual-arrow-edit"
                and integrity.get("status") == "failed"
                and crop.get("status") == "verified"
            )
            if integrity.get("status") != "pass" and not manual_override:
                if strict_canvas:
                    raise FigureCanvasApprovalBlocked(
                        "The redraw canvas aspect ratio does not match the selected source. Use the SVG Crop Canvas workflow or redraw it before approval.",
                        details={
                            "figure_id": figure_id,
                            "source_size": integrity.get("source_size"),
                            "output_size": integrity.get("output_size"),
                        },
                    )
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
            ) = self._approve_rows(principal, project_id, {figure_id}, strict_canvas=True)
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
            ) = self._approve_rows(principal, project_id, all_ids, strict_canvas=False)
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
