"""Versioned Matrix, outline, and Blueprint routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Header, Query, status

from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.job_handlers.stage_execution import queue_fact_revision
from review_writer_api.planning_jobs import queue_matrix_enrichment
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal
from review_writer_api.workflow_schemas import (
    BlueprintRestoreRequest,
    BlueprintGenerateRequest,
    BlueprintConfirmRequest,
    MatrixLimitedModeRequest,
    MatrixRowUpdateRequest,
    OutlineRecommendationRequest,
    OutlineSaveRequest,
    ReferenceOutlineUploadRequest,
)


def build_planning_router(
    principal_dependency: Callable[..., Principal],
    planning_service: PlanningService,
    job_service: JobService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/planning", tags=["planning"]
    )

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
        saved = planning_service.update_matrix_row(
            principal,
            project_id,
            paper_id,
            revision=payload.revision,
            main_content=payload.main_content,
            most_relevant_figure=payload.most_relevant_figure,
            scientific_facts=payload.scientific_facts,
            mark_complete=payload.mark_complete,
        )
        return queue_fact_revision(planning_service, job_service, principal, project_id, saved)

    @router.post("/matrix/enrichment/jobs", status_code=status.HTTP_202_ACCEPTED)
    def enrich_matrix(
        project_id: str,
        force: bool = False,
        paper_ids: list[str] | None = Query(default=None),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        job = queue_matrix_enrichment(planning_service, job_service, principal, project_id,
            force=force, paper_ids=paper_ids, idempotency_key=idempotency_key.strip() or str(uuid.uuid4()))
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

    @router.post("/outline/recommendations")
    async def recommend_outline_papers(
        project_id: str,
        payload: OutlineRecommendationRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return await planning_service.recommend_outline_papers(
            principal,
            project_id,
            revision=payload.revision,
            outline_md=payload.outline_md,
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

    @router.post("/blueprint", status_code=status.HTTP_202_ACCEPTED)
    @router.post("/blueprint/jobs", status_code=status.HTTP_202_ACCEPTED)
    def plan_blueprint(
        project_id: str, payload: BlueprintGenerateRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        """Both public entries submit the same source-grounded argument planning job."""
        if payload.draft_quality_artifact_id:
            raise WorkflowConflict("Draft argument repairs now run in Draft optimization. Refresh the page and generate a joint revision there.")
        prepared = planning_service.blueprint_job_payload(principal, project_id, revision=payload.revision)
        request_key = idempotency_key.strip() or str(uuid.uuid4())
        try:
            job = job_service.submit(
                principal, scope="project", project_id=project_id, job_type="planning.blueprint",
                idempotency_key=request_key, operation_key="blueprint-planning", payload=prepared,
            )
        except WorkflowConflict as conflict:
            existing_id = conflict.details.get("existing_job_id")
            if not existing_id:
                raise
            existing = job_service.status(principal, existing_id)
            # Preparation adds timestamps and checkpoints. For an already scoped
            # idempotency key, compare the client request, not those derived fields.
            if (existing.idempotency_key != request_key
                    or existing.payload.get("blueprint_revision") != payload.revision
                    or existing.payload.get("draft_repair_input_artifacts") != prepared.get("draft_repair_input_artifacts")):
                raise
            job = existing
        return _job_response(job)

    @router.post("/blueprint/confirm")
    def confirm_blueprint(
        project_id: str,
        payload: BlueprintConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.confirm_blueprint(
            principal, project_id, revision=payload.revision, artifact_id=payload.artifact_id
        )

    @router.post("/blueprint/restore")
    def restore_blueprint(
        project_id: str,
        payload: BlueprintRestoreRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return planning_service.restore_blueprint(
            principal,
            project_id,
            revision=payload.revision,
            artifact_id=payload.artifact_id,
        )

    return router
