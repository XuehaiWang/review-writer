"""Versioned Matrix, outline, and Blueprint routes."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    BlueprintGenerateRequest,
    BlueprintConfirmRequest,
    MatrixLimitedModeRequest,
    MatrixRowUpdateRequest,
    OutlineSaveRequest,
    ReferenceOutlineUploadRequest,
)


def build_planning_router(
    principal_dependency: Callable[..., Principal],
    planning_service: PlanningService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/planning", tags=["planning"]
    )
    enrichment_builder = dict(handlers or {}).get("matrix.enrich")
    if enrichment_builder is not None:

        def matrix_enrichment_handler(context, payload):
            payload = dict(payload)
            principal = Principal(context.user_id, frozenset({Role.USER}))
            if payload.get("prepare_on_start"):
                expected_artifact_id = str(
                    payload.get("source_matrix_artifact_id") or ""
                )
                payload = planning_service.matrix_enrichment_payload(
                    principal, str(context.project_id)
                )
                if (
                    expected_artifact_id
                    and payload.get("source_matrix_artifact_id") != expected_artifact_id
                ):
                    raise WorkflowConflict(
                        "Matrix changed before scientific fact extraction started."
                    )
            if context.retry_of_job_id:
                source_job = context.repository.get_job(
                    context.user_id, context.retry_of_job_id
                )
                source_result = (source_job.result or {}) if source_job is not None else {}
                checkpoint = source_result.get("matrix_enrichment_checkpoint")
                if not isinstance(checkpoint, dict):
                    # The progress callback persists checkpoints before the
                    # handler publishes its final result.  A publish conflict
                    # therefore leaves the scientifically useful checkpoint
                    # under this generic progress key.
                    checkpoint = source_result.get("section_checkpoint")
                if isinstance(checkpoint, dict):
                    payload["resume_checkpoint"] = checkpoint
            total = int(payload.get("pending_paper_count") or 0)
            context.report_progress(0, total)
            if not total:
                return {
                    "project_id": str(context.project_id),
                    "status": "current",
                    "message": "Matrix scientific facts are already current.",
                }
            if not int(payload.get("fulltext_candidate_paper_count") or 0):
                return {
                    "project_id": str(context.project_id),
                    "status": "awaiting_fulltext_index",
                    "pending_paper_count": total,
                    "message": "Build full-text indexes before extracting Matrix scientific facts.",
                }
            built = enrichment_builder(context, payload)
            context.checkpoint()
            result = planning_service.publish_matrix_enrichment(
                principal, str(context.project_id), payload, built
            )
            context.report_progress(total, total)
            return result

        job_service.register_handler("matrix.enrich", matrix_enrichment_handler)

    @router.get("")
    def get_planning(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.get(principal, project_id)

    @router.put("/matrix/{paper_id}")
    def update_matrix_row(
        project_id: str,
        paper_id: str,
        payload: MatrixRowUpdateRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.update_matrix_row(
            principal,
            project_id,
            paper_id,
            revision=payload.revision,
            main_content=payload.main_content,
            most_relevant_figure=payload.most_relevant_figure,
            scientific_facts=payload.scientific_facts,
            mark_complete=payload.mark_complete,
        )

    @router.post("/matrix/enrichment/jobs", status_code=status.HTTP_202_ACCEPTED)
    def enrich_matrix(
        project_id: str,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        payload = planning_service.matrix_enrichment_payload(principal, project_id)
        if not int(payload.get("pending_paper_count") or 0):
            raise WorkflowConflict("Matrix scientific facts are already current.")
        if not int(payload.get("fulltext_candidate_paper_count") or 0):
            raise WorkflowConflict(
                "Build full-text indexes before extracting Matrix scientific facts."
            )
        job = job_service.submit(
            principal,
            scope="project",
            project_id=project_id,
            job_type="matrix.enrich",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            operation_key="matrix-enrichment",
            payload=payload,
        )
        return _job_response(job)

    @router.post("/matrix/enrichment/limited-mode")
    def continue_matrix_limited_mode(
        project_id: str,
        payload: MatrixLimitedModeRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.confirm_matrix_limited_mode(
            principal, project_id, revision=payload.revision
        )

    @router.put("/outline")
    def save_outline(
        project_id: str,
        payload: OutlineSaveRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.save_outline(
            principal,
            project_id,
            revision=payload.revision,
            outline_style=payload.outline_style,
            outline_md=payload.outline_md,
            scope_contract=payload.scope_contract,
            manual="outline_md" in payload.model_fields_set,
        )

    @router.post("/reference-outlines", status_code=status.HTTP_201_CREATED)
    def register_reference_outline(
        project_id: str,
        payload: ReferenceOutlineUploadRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.register_reference(
            principal,
            project_id,
            revision=payload.revision,
            filename=payload.filename,
            content_base64=payload.content_base64,
        )

    @router.post("/blueprint")
    def generate_blueprint(
        project_id: str,
        payload: BlueprintGenerateRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.generate_blueprint(
            principal, project_id, revision=payload.revision
        )

    @router.post("/blueprint/confirm")
    def confirm_blueprint(
        project_id: str,
        payload: BlueprintConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.confirm_blueprint(
            principal, project_id, revision=payload.revision
        )

    return router
