"""Versioned Draft assembly, editing, quality, rewrite, and approval endpoints."""

from __future__ import annotations

import uuid
import asyncio
import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import StreamingResponse

from review_writer_api.domain_services.drafts import DraftsService
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal
from review_writer_api.workflow_schemas import (
    DraftApprovalRequest,
    DraftDialogueRequest,
    DraftSectionDialogueRequest,
    DraftParagraphSaveRequest,
    DraftRestoreRequest,
    DraftTextSaveRequest,
)


def build_drafts_router(
    principal_dependency: Callable[..., Principal],
    drafts_service: DraftsService,
    job_service: JobService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/draft", tags=["draft"])

    @router.get("/section-dialogues/{section_id}")
    def get_section_dialogue(project_id: str, section_id: str, principal: Principal = Depends(principal_dependency)):
        return drafts_service.section_dialogue_history(principal, project_id, section_id)

    @router.get("/section-dialogues/{section_id}/versions")
    def section_versions(project_id: str, section_id: str, principal: Principal = Depends(principal_dependency)):
        return drafts_service.section_versions(principal, project_id, section_id)

    @router.get("/section-dialogues/{section_id}/versions/{artifact_id}")
    def section_version(project_id: str, section_id: str, artifact_id: str, principal: Principal = Depends(principal_dependency)):
        return drafts_service._section_version(principal, project_id, section_id, artifact_id)

    @router.post("/section-dialogues/{section_id}", status_code=status.HTTP_202_ACCEPTED)
    def start_section_dialogue(project_id: str, section_id: str, payload: DraftSectionDialogueRequest,
                               idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                               principal: Principal = Depends(principal_dependency)):
        key = idempotency_key.strip() or str(uuid.uuid4())
        job_payload, existing = drafts_service.section_dialogue_payload(principal, project_id, section_id,
            **payload.model_dump(), idempotency_key=key)
        return _job_response(existing or job_service.submit(principal, scope="project", project_id=project_id,
            job_type="draft.optimize", operation_key=f"section:{section_id}", idempotency_key=key, payload=job_payload))

    @router.get("/section-dialogues/{section_id}/stream/{job_id}")
    def stream_section_dialogue(project_id: str, section_id: str, job_id: str, request: Request,
                               principal: Principal = Depends(principal_dependency)):
        # Authenticate and scope-check before sending any streaming headers.
        snapshot = drafts_service.section_stream_snapshot(principal, project_id, section_id, job_id)

        async def events():
            current, previous = snapshot, None
            # Full text snapshots make reconnection idempotent: never append twice.
            for tick in range(240):
                if await request.is_disconnected():
                    return
                encoded = json.dumps(current, ensure_ascii=False)
                if encoded != previous:
                    yield f"event: snapshot\ndata: {encoded}\n\n"
                    previous = encoded
                elif tick % 20 == 0:
                    yield ": keepalive\n\n"
                if current["status"] not in {"queued", "running", "cancel_requested"}:
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)
                current = await asyncio.to_thread(drafts_service.section_stream_snapshot,
                    principal, project_id, section_id, job_id)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @router.get("/dialogues/{paragraph_key}")
    def get_dialogue(project_id: str, paragraph_key: str, principal: Principal = Depends(principal_dependency)):
        return drafts_service.dialogue_history(principal, project_id, paragraph_key)

    @router.post("/dialogues/{paragraph_key}", status_code=status.HTTP_202_ACCEPTED)
    def start_dialogue(project_id: str, paragraph_key: str, payload: DraftDialogueRequest,
                       idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                       principal: Principal = Depends(principal_dependency)):
        key = idempotency_key.strip() or str(uuid.uuid4())
        job_payload, existing = drafts_service.dialogue_payload(principal, project_id, paragraph_key,
            **payload.model_dump(), idempotency_key=key)
        return _job_response(existing or job_service.submit(principal, scope="project", project_id=project_id,
            job_type="draft.rewrite", operation_key=f"paragraph:{paragraph_key}", idempotency_key=key, payload=job_payload))

    @router.post("/dialogue-candidates/{candidate_id}/{decision}")
    def decide_dialogue(project_id: str, candidate_id: str, decision: str,
                        principal: Principal = Depends(principal_dependency)):
        return drafts_service.decide_dialogue(principal, project_id, candidate_id, decision=decision)

    @router.post("/dialogue-batch", status_code=status.HTTP_202_ACCEPTED)
    def start_dialogue_batch(project_id: str, idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                             principal: Principal = Depends(principal_dependency)):
        current = drafts_service.get(principal, project_id)
        key = idempotency_key.strip() or str(uuid.uuid4())
        existing = drafts_service.repository.list_project_jobs(principal.user_id, project_id,
            job_type="draft.optimize", idempotency_key=key, limit=1)
        if existing:
            return _job_response(existing[0])
        from review_writer_api.errors import WorkflowValidationError
        if not current["paragraphs"] or current["freshness"]["upstream_stale"]:
            raise WorkflowValidationError("A current saved Draft is required.")
        return _job_response(job_service.submit(principal, scope="project", project_id=project_id,
            job_type="draft.optimize", idempotency_key=key, payload={"project_id": project_id,
                "revision_mode": "dialogue_batch", "paragraphs": current["paragraphs"],
                "message": "Analyze and improve this paragraph using the available original-source evidence. Keep the original when no defensible improvement is needed."}))
    @router.get("")
    def get_draft(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.get(principal, project_id)

    @router.post("/assemble")
    def assemble_draft(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.assemble(principal, project_id)

    @router.put("")
    def save_draft(
        project_id: str,
        payload: DraftTextSaveRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.save_text(
            principal,
            project_id,
            text=payload.text,
            revision=payload.revision,
        )

    @router.put("/paragraphs/{paragraph_id}")
    def save_paragraph(
        project_id: str,
        paragraph_id: str,
        payload: DraftParagraphSaveRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.save_paragraph(
            principal,
            project_id,
            paragraph_id,
            text=payload.text,
            revision=payload.revision,
            base_text_sha256=payload.base_text_sha256,
        )

    @router.post("/restore")
    def restore_draft(
        project_id: str,
        payload: DraftRestoreRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.restore(
            principal,
            project_id,
            artifact_id=payload.artifact_id,
            revision=payload.revision,
        )

    # Retired HTTP writes must not silently create scored or auto-applied jobs.
    # Old task executors were removed after the deployment queue drained.
    def retired_revision(project_id: str, principal: Principal = Depends(principal_dependency)):
        from fastapi import HTTPException
        drafts_service._owned_project(principal, project_id)
        raise HTTPException(status_code=410,
            detail="Scored revision was replaced by paragraph dialogue and batch analysis. Refresh Draft to use the current workflow.")

    for path in ("/evaluation-jobs", "/optimization-jobs", "/paragraphs/{paragraph_id}/rewrite-jobs",
                 "/optimization-proposals/{proposal_id}/{decision}", "/rewrite-candidates/{candidate_id}/accept-jobs",
                 "/rewrite-candidates/{candidate_id}/{decision}"):
        router.add_api_route(path, retired_revision, methods=["POST"], deprecated=True)

    @router.post("/approve")
    def approve_draft(
        project_id: str,
        payload: DraftApprovalRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.approve(
            principal,
            project_id,
            revision=payload.revision,
            override_low_score=payload.override_low_score,
            override_reason=payload.override_reason,
        )

    return router
