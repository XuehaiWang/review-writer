"""Native versioned Discovery routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, status

from review_writer_api.domain_services.discovery import DiscoveryService
from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_service import JobService
from review_writer_api.planning_jobs import queue_matrix_enrichment
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal
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
    planning_service: PlanningService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/discovery", tags=["discovery"])
    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    def run_discovery(
        project_id: str,
        payload: DiscoverySearchRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        discovery_service._owned_project(principal, project_id)
        current = job_service.repository.get_current_job(
            principal.user_id,
            scope="project",
            project_id=project_id,
            job_type="discovery.search",
        )
        if current is not None and current.status in {
            "queued",
            "running",
            "cancel_requested",
        }:
            # A refreshed browser may no longer know the in-memory job id.
            # Reattach it to the persisted job instead of surfacing a conflict
            # or submitting a duplicate provider request.
            return _job_response(current)
        payload_data = discovery_service.search_payload(principal, project_id, payload.model_dump())
        try:
            job = job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="discovery.search",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload=payload_data,
            )
        except WorkflowConflict as exc:
            # Close the narrow race between the current-job lookup and atomic
            # job creation. Only conflicts that identify a live current job are
            # recoverable; idempotency/payload conflicts still propagate.
            current_job_id = str(exc.details.get("current_job_id") or "")
            if not current_job_id:
                raise
            current = job_service.status(principal, current_job_id)
            if current.status not in {"queued", "running", "cancel_requested"}:
                raise
            return _job_response(current)
        return _job_response(job)

    @router.get("/jobs/current")
    def current_discovery_job(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        discovery_service._owned_project(principal, project_id)
        current = job_service.repository.get_current_job(
            principal.user_id,
            scope="project",
            project_id=project_id,
            job_type="discovery.search",
        )
        if current is None:
            return {"active_job": None, "latest_job": None}
        response = _job_response(current).model_dump()
        return {
            "active_job": (
                response
                if current.status in {"queued", "running", "cancel_requested"}
                else None
            ),
            "latest_job": response,
        }

    @router.get("")
    def get_discovery(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.get(principal, project_id)

    @router.get("/fulltext-check")
    def check_fulltext(
        project_id: str,
        candidate_id: str = Query(min_length=1, max_length=2048),
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.check_fulltext(principal, project_id, candidate_id)

    @router.put("")
    def save_discovery(
        project_id: str,
        payload: DiscoveryReviewSaveRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return discovery_service.save(
            principal,
            project_id,
            payload.revision,
            payload.results,
            coverage_decision=payload.coverage_decision,
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
        result = discovery_service.confirm(
            principal, project_id, payload.revision
        )
        if planning_service is not None:
            job = queue_matrix_enrichment(planning_service, job_service, principal, project_id)
            result["matrix_analysis_job"] = _job_response(job).model_dump()
        return result

    return router
