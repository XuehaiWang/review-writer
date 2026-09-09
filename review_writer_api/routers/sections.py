"""Versioned section generation, progress, report, and handoff routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal
from review_writer_api.workflow_schemas import (
    SectionsConfirmRequest,
    SectionsGenerateRequest,
)


def build_sections_router(
    principal_dependency: Callable[..., Principal],
    sections_service: SectionsService,
    job_service: JobService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/sections", tags=["sections"]
    )

    @router.get("")
    def get_sections(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return sections_service.get(principal, project_id)

    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    def generate_sections(
        project_id: str,
        _payload: SectionsGenerateRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        payload = sections_service.generation_payload(principal, project_id)
        job = job_service.submit(
            principal,
            scope="project",
            project_id=project_id,
            job_type="sections.generate",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            payload=payload,
        )
        return _job_response(job)

    @router.post("/confirm")
    def confirm_sections(
        project_id: str,
        payload: SectionsConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return sections_service.confirm(
            principal, project_id, revision=payload.revision
        )

    return router
