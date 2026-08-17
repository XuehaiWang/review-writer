"""Versioned source review, redraw, SVG editing, approval, and figure gates."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.figures import (
    FigureSafetyBlocked,
    FiguresService,
    SAFETY_ERROR,
)
from review_writer_api.errors import WorkflowError
from review_writer_api.job_service import (
    JobCancellationRequested,
    JobService,
    JobShutdownRequested,
)
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    FigureConfirmRequest,
    FigureFullSvgRequest,
    FigureManualEditRequest,
    FigureRedrawRequest,
    FigureReviewConfirmRequest,
    FigureReviewSelectionRequest,
)


def build_figures_router(
    principal_dependency: Callable[..., Principal],
    figures_service: FiguresService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/figures", tags=["figures"])
    builder = dict(handlers or {}).get("figures.redraw")
    if builder is not None:

        def redraw_handler(context, payload):
            figure_ids = list(payload.get("figure_ids") or [])
            execution_payload = {**payload, "producer_job_id": context.job_id}
            context.report_progress(0, len(figure_ids))
            principal = Principal(context.user_id, frozenset({Role.USER}))
            results: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for index, figure_id in enumerate(figure_ids, start=1):
                context.checkpoint()
                item = figures_service.resolve_redraw_item(
                    principal,
                    str(context.project_id),
                    execution_payload,
                    str(figure_id),
                )
                try:
                    built = builder(context, item)
                    context.checkpoint()
                    result = figures_service.publish_redraw(
                        principal,
                        str(context.project_id),
                        execution_payload,
                        built,
                    )
                except (JobCancellationRequested, JobShutdownRequested):
                    raise
                except Exception as exc:
                    if SAFETY_ERROR.search(str(exc)):
                        normalized: Exception = FigureSafetyBlocked(
                            "The image provider blocked this chemistry figure during safety review. No output was admitted."
                        )
                    else:
                        normalized = exc
                    errors.append(
                        {
                            "figure_id": str(figure_id),
                            "error_code": (
                                normalized.code
                                if isinstance(normalized, WorkflowError)
                                else "FIGURE_REDRAW_FAILED"
                            ),
                            "error": str(normalized),
                        }
                    )
                    context.report_partial_result(
                        {
                            "figure_count": len(results),
                            "figure_ids": figure_ids,
                            "outputs": results,
                            "errors": errors,
                        }
                    )
                    if len(figure_ids) == 1:
                        if normalized is exc:
                            raise
                        raise normalized from exc
                else:
                    results.append(result)
                    context.report_partial_result(
                        {
                            "figure_count": len(results),
                            "figure_ids": figure_ids,
                            "outputs": results,
                            "errors": errors,
                        }
                    )
                context.repository.update_job_progress(
                    context.job_id, index, len(figure_ids)
                )
            return {
                "figure_count": len(results),
                "figure_ids": figure_ids,
                "outputs": results,
                "errors": errors,
            }

        job_service.register_handler("figures.redraw", redraw_handler)

    @router.get("/review")
    def get_review(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.get_review(principal, project_id)

    @router.put("/review/{paper_id}")
    def save_review_selection(
        project_id: str,
        paper_id: str,
        payload: FigureReviewSelectionRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.save_review_selection(
            principal,
            project_id,
            paper_id,
            revision=payload.revision,
            candidate_index=payload.candidate_index,
            review_note=payload.review_note,
        )

    @router.post("/review/confirm")
    def confirm_review(
        project_id: str,
        payload: FigureReviewConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.confirm_review(
            principal, project_id, revision=payload.revision
        )

    @router.post("/review/sync")
    def sync_review_inputs(
        project_id: str,
        payload: FigureReviewConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.sync_review_inputs(
            principal, project_id, revision=payload.revision
        )

    @router.get("")
    def get_figures(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.get(principal, project_id)

    def submit_redraw(
        project_id: str,
        payload: FigureRedrawRequest,
        idempotency_key: str,
        principal: Principal,
        origin: str,
    ):
        job_payload = figures_service.redraw_job_payload(
            principal,
            project_id,
            figure_ids=list(payload.figure_ids),
            figure_type=payload.figure_type,
            retry_of_job_id=payload.retry_of_job_id,
            origin=origin,
        )
        job = job_service.submit(
            principal,
            scope="project",
            project_id=project_id,
            job_type="figures.redraw",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            payload=job_payload,
            retry_of_job_id=payload.retry_of_job_id,
            operation_key=(
                f"figure:{job_payload['figure_ids'][0]}"
                if origin == "single" and len(job_payload["figure_ids"]) == 1
                else ""
            ),
        )
        return _job_response(job)

    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    def redraw_batch(
        project_id: str,
        payload: FigureRedrawRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        return submit_redraw(project_id, payload, idempotency_key, principal, "batch")

    @router.post("/{figure_id}/jobs", status_code=status.HTTP_202_ACCEPTED)
    def redraw_one(
        project_id: str,
        figure_id: str,
        payload: FigureRedrawRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        requested = payload.model_copy(update={"figure_ids": [figure_id]})
        return submit_redraw(project_id, requested, idempotency_key, principal, "single")

    @router.post("/{figure_id}/full-svg")
    def create_full_svg(
        project_id: str,
        figure_id: str,
        payload: FigureFullSvgRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.create_full_svg(
            principal, project_id, figure_id, base_mode=payload.base_mode
        )

    @router.post("/{figure_id}/manual-edit")
    def save_manual_edit(
        project_id: str,
        figure_id: str,
        payload: FigureManualEditRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.save_manual_edit(
            principal,
            project_id,
            figure_id,
            image_png_data_url=payload.image_png_data_url,
            operations=payload.operations,
            base_mode=payload.base_mode,
            editable_svg=payload.editable_svg,
            full_vector_svg=payload.full_vector_svg,
        )

    @router.post("/{figure_id}/approve")
    def approve_one(
        project_id: str,
        figure_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.approve(principal, project_id, figure_id)

    @router.post("/approve-successful")
    def approve_successful(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.approve_successful(principal, project_id)

    @router.post("/confirm")
    def confirm_figures(
        project_id: str,
        payload: FigureConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return figures_service.confirm(
            principal, project_id, revision=payload.revision
        )

    return router
