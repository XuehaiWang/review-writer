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
            total = len(payload.get("tasks") or [])
            context.report_progress(0, total)
            attempts = 0
            while attempts < 3:
                attempts += 1
                try:
                    built = builder(context, payload)
                    break
                except ScientificRunError:
                    raise
                except Exception as exc:
                    if not TRANSIENT_PROVIDER_ERROR.search(str(exc)) or attempts >= 3:
                        if TRANSIENT_PROVIDER_ERROR.search(str(exc)):
                            raise SectionProviderUnavailable(
                                "The section-writing provider remained unavailable after three attempts. Try again later or choose another configured model.",
                                details={"attempts": attempts},
                            ) from exc
                        raise
                    context.checkpoint()
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.checkpoint()
            result = sections_service.publish_generation(
                principal,
                str(context.project_id),
                payload,
                built,
                attempts=attempts,
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
