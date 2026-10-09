"""Native versioned Discovery routes."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.discovery import DiscoveryService
from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.job_service import JobService
from review_writer_api.planning_jobs import queue_matrix_enrichment
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_schemas import (
    DiscoveryConfirmRequest,
    DiscoveryDownloadRequest,
    DiscoveryFulltextCheckRequest,
    DiscoveryReviewSaveRequest,
    DiscoverySearchRequest,
)


# One request covers a page of candidates; the probe pool bounds concurrency.
MAX_FULLTEXT_CHECK_CANDIDATES = 50


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

    @router.post("/fulltext-check")
    def check_fulltext(
        project_id: str,
        payload: DiscoveryFulltextCheckRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        """Probe open-PDF availability for the listed candidates.

        This is a POST because it may perform bounded outbound network work; a
        GET must stay a pure read. One request covers a page of candidates.
        """
        candidate_ids = [
            item.strip() for item in payload.candidate_ids
            if item.strip()
        ][:MAX_FULLTEXT_CHECK_CANDIDATES]
        if not candidate_ids:
            raise WorkflowValidationError("At least one candidate is required.")
        return {"checks": discovery_service.check_fulltext_many(principal, project_id, candidate_ids)}

    @router.post("/download-jobs", status_code=status.HTTP_202_ACCEPTED)
    def download_discovery_candidate(
        project_id: str,
        payload: DiscoveryDownloadRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        principal.require(Permission.PROJECT_WRITE)
        candidate = discovery_service.download_candidate(
            principal, project_id, payload.candidate_id, payload.revision
        )
        operation_key = f"project:{project_id}"

        def current_download():
            current = job_service.repository.get_current_job(
                principal.user_id,
                scope="library",
                job_type="discovery.acquire",
                operation_key=operation_key,
            )
            if current is None or current.status not in {"queued", "running", "cancel_requested"}:
                return None
            candidates = current.payload.get("candidates") or []
            if current.payload.get("discovery_revision") == payload.revision and any(
                isinstance(item, dict) and str(item.get("candidate_id") or "") == payload.candidate_id
                for item in candidates
            ):
                return current
            raise WorkflowConflict("Another paper is being downloaded. Wait for it to finish, then retry.")

        current = current_download()
        if current is not None:
            return _job_response(current)
        try:
            job = job_service.submit(
                principal,
                scope="library",
                project_id=None,
                # Discovery owns the whole acquisition chain: download the open
                # PDF, then refresh this candidate. Library is never told about
                # Discovery, and no back-channel payload field is needed.
                job_type="discovery.acquire",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload={
                    "candidates": [candidate],
                    "discovery_revision": payload.revision,
                    "project_id": project_id,
                },
                operation_key=operation_key,
            )
        except WorkflowConflict as exc:
            if not exc.details.get("current_job_id"):
                raise
            current = current_download()
            if current is None:
                raise
            return _job_response(current)
        return _job_response(job)

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

    # Selection is edited in the browser and saved once with the review, so no
    # per-candidate selection endpoints exist.

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
