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
    FinalBibliographyCorrectionRequest,
    FinalFigureReviewRequest,
)


def build_final_router(
    principal_dependency: Callable[..., Principal],
    final_service: FinalService,
    job_service: JobService,
    library_service=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/final", tags=["final"])
    @router.get("")
    def get_final(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return final_service.get(principal, project_id)

    @router.get("/figures/{figure_id}/review")
    def get_figure_review(project_id: str, figure_id: str, principal: Principal = Depends(principal_dependency)):
        return final_service.figure_review(principal, project_id, figure_id)

    @router.put("/figures/{figure_id}/review")
    def save_figure_review(project_id: str, figure_id: str, payload: FinalFigureReviewRequest,
                          principal: Principal = Depends(principal_dependency)):
        return final_service.save_figure_review(principal, project_id, figure_id, payload)

    def reference_record(principal, project_id, paper_id):
        from review_writer_api.errors import WorkflowNotFound
        current = final_service.get(principal, project_id)
        paper_ids = (current.get("release") or {}).get("source_paper_ids") or []
        if library_service is None or paper_id not in paper_ids:
            raise WorkflowNotFound("该论文不在当前终稿的引用文献中。")
        return library_service.get(principal, paper_id)

    @router.get("/references/{paper_id}/bibliography")
    def get_reference(project_id: str, paper_id: str, principal: Principal = Depends(principal_dependency)):
        record = reference_record(principal, project_id, paper_id)
        return {"metadata": record.metadata, "metadata_artifact_id": record.artifact_ids.get("metadata", "")}

    @router.put("/references/{paper_id}/bibliography")
    def correct_reference(project_id: str, paper_id: str, payload: FinalBibliographyCorrectionRequest,
                          principal: Principal = Depends(principal_dependency)):
        from review_writer_core.bibliography_audit import refresh_edited_bibliography, bibliography_field_readiness
        from review_writer_api.security import Permission
        principal.require(Permission.PROJECT_WRITE)
        record = reference_record(principal, project_id, paper_id)
        metadata = dict(record.metadata)
        for field, value in payload.fields.items():
            metadata[field] = {"value": value, "human_checked": True, "confidence": 1,
                               "source": "human_review", "evidence": {"location": payload.source_location}}
        audit = refresh_edited_bibliography(record.metadata, metadata, record.bibliography_audit)
        # Resolve only fields explicitly checked here, never unrelated conflicts.
        audit["conflicts"] = [
            {**row, "status": "resolved", "resolved_value": payload.fields[row["field"]]} if isinstance(row, dict) and row.get("field") in payload.fields else row
            for row in audit.get("conflicts") or []
        ]
        audit["unresolved_conflicts"] = [row for row in audit.get("unresolved_conflicts") or []
                                          if not isinstance(row, dict) or row.get("field") not in payload.fields]
        audit["field_readiness"] = bibliography_field_readiness(metadata, audit)
        audit["automatic_resolution_missing_fields"] = list(dict.fromkeys(
            audit["field_readiness"]["missing_fields"] + audit["field_readiness"]["polluted_fields"]))
        audit["manual_evidence"] = {"evidence_type": "user_confirmation", "location": payload.source_location}
        if audit["field_readiness"]["ready"]:
            audit.update(manual_review_status="resolved", resolved_by="human",
                         resolved_fields=list(payload.fields))
        saved = library_service._persist_metadata_and_audit(principal, paper_id, metadata,
            bibliography_audit=audit, expected_metadata_artifact_id=payload.metadata_artifact_id)
        return {"saved": True, "metadata_artifact_id": saved.artifact_ids.get("metadata", ""),
                "requires_final_sync": True, "authors_changed": "authors" in payload.fields}

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
