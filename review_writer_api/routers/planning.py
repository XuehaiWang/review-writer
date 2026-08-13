"""Versioned Matrix, outline, and Blueprint routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, status

from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.security import Principal
from review_writer_api.workflow_schemas import (
    BlueprintGenerateRequest,
    BlueprintConfirmRequest,
    MatrixRowUpdateRequest,
    OutlineSaveRequest,
    ReferenceOutlineUploadRequest,
)


def build_planning_router(
    principal_dependency: Callable[..., Principal],
    planning_service: PlanningService,
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
        return planning_service.update_matrix_row(
            principal,
            project_id,
            paper_id,
            revision=payload.revision,
            main_content=payload.main_content,
            most_relevant_figure=payload.most_relevant_figure,
            mark_complete=payload.mark_complete,
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
