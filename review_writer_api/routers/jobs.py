"""User-scoped polling, cancellation, and retry endpoints."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, status

from review_writer_api.job_service import JobService, job_payload
from review_writer_api.schemas import JobResponse
from review_writer_api.security import Principal
from review_writer_api.workflow_repository import JobRecord


def _job_response(job: JobRecord) -> JobResponse:
    return JobResponse(**job_payload(job))


def build_job_router(
    principal_dependency: Callable[..., Principal], job_service: JobService
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

    @router.get("/{job_id}", response_model=JobResponse)
    def job_status(
        job_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> JobResponse:
        return _job_response(job_service.status(principal, job_id))

    @router.post("/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(
        job_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> JobResponse:
        return _job_response(job_service.request_cancel(principal, job_id))

    @router.post(
        "/{job_id}/retry",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_job(
        job_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> JobResponse:
        return _job_response(job_service.retry_interrupted(principal, job_id))

    return router
