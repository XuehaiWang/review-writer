"""Versioned section generation, progress, report, and handoff routes."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, Header, status

from review_writer_api.domain_services.sections import (
    SectionProviderUnavailable,
    SectionsService,
)
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.scientific_runner import ScientificRunError
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    SectionsConfirmRequest,
    SectionsGenerateRequest,
)


TRANSIENT_PROVIDER_ERROR = re.compile(
    r"(?:HTTP\s*(?:429|503)|rate[_ -]?limit|service unavailable|temporar(?:y|ily))",
    re.IGNORECASE,
)


def build_sections_router(
    principal_dependency: Callable[..., Principal],
    sections_service: SectionsService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/sections", tags=["sections"]
    )
    builder = dict(handlers or {}).get("sections.generate")
    if builder is not None:

        def section_handler(context, payload):
            payload = dict(payload)
            if context.retry_of_job_id:
                source_job = context.repository.get_job(
                    context.user_id, context.retry_of_job_id
                )
                checkpoint = (
                    (source_job.result or {}).get("section_checkpoint")
                    if source_job is not None
                    else None
                )
                if isinstance(checkpoint, dict):
                    payload["resume_checkpoint"] = checkpoint
            total = len(payload.get("tasks") or [])
            context.report_progress(0, total)
            try:
                built = builder(context, payload)
            except ScientificRunError:
                raise
            except Exception as exc:
                if TRANSIENT_PROVIDER_ERROR.search(str(exc)):
                    raise SectionProviderUnavailable(
                        "The section-writing provider remained unavailable after the gateway exhausted its provider retries. Completed object checkpoints were preserved; retry this job to continue.",
                        details={"attempts": 1, "resume_supported": True},
                    ) from exc
                raise
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.checkpoint()
            result = sections_service.publish_generation(
                principal,
                str(context.project_id),
                payload,
                built,
                attempts=1,
            )
            context.repository.update_job_progress(context.job_id, total, total)
            return result

        job_service.register_handler("sections.generate", section_handler)

    @router.get("")
    def get_sections(
        project_id: str,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return sections_service.get(principal, project_id)

    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    def generate_sections(
        project_id: str,
        _payload: SectionsGenerateRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        payload = sections_service.generation_payload(principal, project_id)
        job = job_service.submit(
            principal,
            scope="project",
            project_id=project_id,
            job_type="sections.generate",
            idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
            payload=payload,
        )
        return _job_response(job)

    @router.post("/confirm")
    def confirm_sections(
        project_id: str,
        payload: SectionsConfirmRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict[str, Any]:
        return sections_service.confirm(
            principal, project_id, revision=payload.revision
        )

    return router
