"""User-scoped polling, cancellation, and retry endpoints."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, status

from review_writer_api.job_service import JobService
from review_writer_api.schemas import JobResponse
from review_writer_api.security import Principal
from review_writer_api.workflow_repository import JobRecord


def _job_response(job: JobRecord) -> JobResponse:
    actions: list[str] = []
    if job.status in {"queued", "running", "cancel_requested"}:
        actions.append("cancel")
    if job.status in JobService.RETRYABLE_STATUSES:
        actions.append("retry")
    return JobResponse(
        id=job.id,
        project_id=job.project_id,
        scope=job.scope,
        job_type=job.job_type,
        status=job.status,
        result=job.result,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        cancellation_requested=job.cancellation_requested,
        error_code=job.error_code,
        error_message=job.error_message,
        retry_of_job_id=job.retry_of_job_id,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        available_actions=actions,
    )


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
