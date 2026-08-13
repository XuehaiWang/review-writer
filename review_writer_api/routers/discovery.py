"""Native versioned Discovery routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.discovery import DiscoveryService
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    DiscoveryConfirmRequest,
    DiscoveryReviewSaveRequest,
    DiscoverySearchRequest,
    DiscoverySelectionRequest,
    DiscoveryTopSelectionRequest,
)


def build_discovery_router(
    principal_dependency: Callable[..., Principal],
    discovery_service: DiscoveryService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/discovery", tags=["discovery"])
    builder = dict(handlers or {}).get("discovery.search")
    if builder is not None:

        def discovery_handler(context, payload):
            context.report_progress(0, 1)
            built = builder(context, payload)
            context.checkpoint()
            principal = Principal(context.user_id, frozenset({Role.USER}))
            result = discovery_service.replace_from_job(
                principal, str(context.project_id), payload, built
            )
            context.report_progress(1, 1)
            return result

        job_service.register_handler("discovery.search", discovery_handler)

    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    def run_discovery(
        project_id: str,
        payload: DiscoverySearchRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        discovery_service._owned_project(principal, project_id)
        payload_data = payload.model_dump()
        job = job_service.submit(
            principal,
            scope="project",
            project_id=project_id,
            job_type="discovery.search",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            payload={
                **payload_data,
                "project_id": project_id,
            },
        )
        return _job_response(job)

    @router.get("")
    def get_discovery(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.get(principal, project_id)

    @router.put("")
    def save_discovery(
        project_id: str,
        payload: DiscoveryReviewSaveRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.save(
            principal, project_id, payload.revision, payload.results
        )

    @router.put("/selection/{paper_id}")
    def select_paper(
        project_id: str,
        paper_id: str,
        payload: DiscoverySelectionRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.select_one(
            principal, project_id, paper_id, payload.selected
        )

    @router.post("/selection/top")
    def select_top(
        project_id: str,
        payload: DiscoveryTopSelectionRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.select_top(principal, project_id, payload.count)

    @router.delete("/selection")
    def clear_selection(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.clear(principal, project_id)

    @router.post("/confirm")
    def confirm(
        project_id: str,
        payload: DiscoveryConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.confirm(
            principal, project_id, payload.revision
        )

    return router
