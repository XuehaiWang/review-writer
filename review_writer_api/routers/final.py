"""Versioned conclusion, overview, final build, validation, and export endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.final import FinalService
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import FinalActionRequest, FinalOverviewTextRequest


def build_final_router(
    principal_dependency: Callable[..., Principal],
    final_service: FinalService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/final", tags=["final"])
    available = dict(handlers or {})

    def register(job_type: str, publisher):
        builder = available.get(job_type)
        if builder is None:
            return

        def handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            built = builder(context, payload)
            context.checkpoint()
            return publisher(principal, str(context.project_id), payload, built)

        job_service.register_handler(job_type, handler)

    register("final.conclusion", final_service.publish_conclusion)
    register("final.overview", final_service.publish_overview)
    register("final.export", final_service.publish_export)

    @router.get("")
    def get_final(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return final_service.get(principal, project_id)

    def submit(
        principal: Principal,
        project_id: str,
        job_type: str,
        idempotency_key: str,
        payload: dict,
    ):
        return _job_response(
            job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type=job_type,
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload=payload,
            )
        )

    @router.post("/conclusion-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_conclusion(
        project_id: str,
        _payload: FinalActionRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        return submit(
            principal,
            project_id,
            "final.conclusion",
            idempotency_key,
            final_service.conclusion_payload(principal, project_id),
        )

    @router.post("/overview-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_overview(
        project_id: str,
        _payload: FinalActionRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        return submit(
            principal,
            project_id,
            "final.overview",
            idempotency_key,
            final_service.overview_payload(principal, project_id),
        )

    @router.put("/overview-text")
    def save_overview_text(
        project_id: str,
        payload: FinalOverviewTextRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return final_service.save_overview_text(
            principal,
            project_id,
            revision=payload.revision,
            title=payload.title,
            subtitle=payload.subtitle,
            labels=list(payload.labels),
        )

    @router.post("/build")
    def build_final(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return final_service.build(principal, project_id)

    @router.post("/export-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_export(
        project_id: str,
        _payload: FinalActionRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        return submit(
            principal,
            project_id,
            "final.export",
            idempotency_key,
            final_service.export_payload(principal, project_id),
        )

    return router
