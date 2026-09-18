"""Versioned conclusion, overview, final build, validation, and export endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.final import FinalService
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal
from review_writer_api.workflow_schemas import (
    FinalActionRequest,
    FinalFrontMatterRequest,
    FinalOverviewTextRequest,
    FinalPdfRequest,
)


def build_final_router(
    principal_dependency: Callable[..., Principal],
    final_service: FinalService,
    job_service: JobService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/final", tags=["final"])
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
        from fastapi import HTTPException
        final_service._owned_project(principal, project_id)
        raise HTTPException(status_code=410, detail="Generate and edit conclusions in Draft, then rebuild Final.")

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

    @router.put("/front-matter")
    def save_front_matter(
        project_id: str,
        payload: FinalFrontMatterRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return final_service.save_front_matter(
            principal,
            project_id,
            revision=payload.revision,
            authors=list(payload.authors),
            affiliations=list(payload.affiliations),
            omitted_fields=list(payload.omitted_fields),
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

    @router.put("/paragraphs/{paragraph_id}")
    def save_paragraph(project_id: str, paragraph_id: str,
                       principal: Principal = Depends(principal_dependency)) -> dict:
        from fastapi import HTTPException
        final_service._owned_project(principal, project_id)
        raise HTTPException(status_code=410, detail="Final manuscripts are read-only. Edit the paragraph in Draft, then rebuild Final.")

    @router.post("/versions/{artifact_id}/restore")
    def restore_version(project_id: str, artifact_id: str,
                        principal: Principal = Depends(principal_dependency)) -> dict:
        from fastapi import HTTPException
        final_service._owned_project(principal, project_id)
        raise HTTPException(status_code=410, detail="Historical Final versions are read-only and remain downloadable. Make changes in Draft.")

    return router
