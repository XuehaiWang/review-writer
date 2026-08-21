"""Versioned conclusion, overview, final build, validation, and export endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.final import FinalService
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    FinalActionRequest,
    FinalOverviewTextRequest,
    FinalPdfRequest,
)


def build_final_router(
    principal_dependency: Callable[..., Principal],
    final_service: FinalService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/final", tags=["final"])
    available = dict(handlers or {})

    def register(job_type: str, publisher, *, progress_total: int):
        builder = available.get(job_type)
        if builder is None:
            return

        def handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.report_progress(1, progress_total)
            built = builder(context, payload)
            context.checkpoint()
            context.report_progress(max(1, progress_total - 1), progress_total)
            result = publisher(principal, str(context.project_id), payload, built)
            # Publication is the commit point. A cancellation arriving after
            # this point must not turn an already-published artifact into a
            # cancelled job, so the final progress update is non-checkpointing.
            context.repository.update_job_progress(
                context.job_id, progress_total, progress_total
            )
            return result

        job_service.register_handler(job_type, handler)

    register("final.conclusion", final_service.publish_conclusion, progress_total=3)
    register("final.overview", final_service.publish_overview, progress_total=4)
    register("final.export", final_service.publish_export, progress_total=3)
    register("final.pdf", final_service.publish_pdf, progress_total=5)

    def build_handler(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        context.report_progress(1, 4)
        current = final_service.build_payload(principal, str(context.project_id))
        if current["source_draft_artifact_id"] != payload.get(
            "source_draft_artifact_id"
        ):
            raise WorkflowConflict(
                "Draft changed while the final-build job was waiting to run."
            )
        context.checkpoint()
        context.report_progress(2, 4)
        result = final_service.build(principal, str(context.project_id))
        context.repository.update_job_progress(context.job_id, 4, 4)
        return result

    job_service.register_handler("final.build", build_handler)

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

    @router.post("/build-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_build(
        project_id: str,
        _payload: FinalActionRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        return submit(
            principal,
            project_id,
            "final.build",
            idempotency_key,
            final_service.build_payload(principal, project_id),
        )

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

    @router.post("/pdf-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_pdf(
        project_id: str,
        payload: FinalPdfRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        return submit(
            principal,
            project_id,
            "final.pdf",
            idempotency_key,
            final_service.pdf_payload(
                principal,
                project_id,
                language_profile=payload.language_profile,
            ),
        )

    return router
