"""Versioned Draft assembly, editing, quality, rewrite, and approval endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from fastapi import APIRouter, Depends, Header, Response, status

from review_writer_api.domain_services.drafts import DraftsService
from review_writer_api.job_service import JobService
from review_writer_api.routers.jobs import _job_response
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_schemas import (
    DraftApprovalRequest,
    DraftEvaluationRequest,
    DraftOptimizationRequest,
    DraftParagraphSaveRequest,
    DraftRestoreRequest,
    DraftRewriteDecisionRequest,
    DraftRewriteRequest,
    DraftTextSaveRequest,
)


def build_drafts_router(
    principal_dependency: Callable[..., Principal],
    drafts_service: DraftsService,
    job_service: JobService,
    handlers: Mapping[str, Callable] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/draft", tags=["draft"])
    available = dict(handlers or {})

    evaluate_builder = available.get("draft.evaluate")
    if evaluate_builder is not None:
        def evaluate_handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.report_progress(1, 3)
            built = evaluate_builder(context, payload)
            context.checkpoint()
            context.report_progress(2, 3)
            result = drafts_service.publish_evaluation(
                principal, str(context.project_id), payload, built
            )
            context.report_progress(3, 3)
            return result

        job_service.register_handler("draft.evaluate", evaluate_handler)

    rewrite_builder = available.get("draft.rewrite")
    if rewrite_builder is not None:
        def rewrite_handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.report_progress(1, 4)
            built = rewrite_builder(context, payload)
            context.checkpoint()
            result = drafts_service.publish_rewrite_candidate(
                principal, str(context.project_id), payload, built
            )
            context.report_progress(4, 4)
            return result

        job_service.register_handler("draft.rewrite", rewrite_handler)

    accept_rewrite_builder = available.get("draft.accept-rewrite")
    if accept_rewrite_builder is not None:
        def accept_rewrite_handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.report_progress(1, 2)
            stored_evaluation = payload.get("candidate_evaluation")
            built = (
                dict(stored_evaluation)
                if isinstance(stored_evaluation, dict)
                and stored_evaluation.get("evaluation_scope") == "single_paragraph"
                else accept_rewrite_builder(context, payload)
            )
            context.checkpoint()
            result = drafts_service.publish_accepted_rewrite(
                principal, str(context.project_id), payload, built
            )
            context.report_progress(2, 2)
            return result

        job_service.register_handler("draft.accept-rewrite", accept_rewrite_handler)

    optimize_builder = available.get("draft.optimize")
    if optimize_builder is not None:
        def optimize_handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            context.report_progress(1, 5)
            built = optimize_builder(context, payload)
            context.checkpoint()
            context.report_progress(4, 5)
            result = drafts_service.publish_optimization(
                principal, str(context.project_id), payload, built
            )
            context.report_progress(5, 5)
            return result

        job_service.register_handler("draft.optimize", optimize_handler)

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

    @router.post("/evaluation-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_evaluation(
        project_id: str,
        payload: DraftEvaluationRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        job_payload = drafts_service.evaluation_payload(
            principal,
            project_id,
            goal=payload.goal,
            paragraph_goal=payload.paragraph_goal,
            max_iterations=payload.max_iterations,
            min_case_words=payload.min_case_words,
            max_case_words=payload.max_case_words,
        )
        return _job_response(
            job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="draft.evaluate",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload=job_payload,
            )
        )

    @router.post("/optimization-jobs", status_code=status.HTTP_202_ACCEPTED)
    def start_optimization(
        project_id: str,
        payload: DraftOptimizationRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        job_payload = drafts_service.evaluation_payload(
            principal,
            project_id,
            goal=payload.goal,
            paragraph_goal=payload.paragraph_goal,
            max_iterations=payload.max_iterations,
            min_case_words=payload.min_case_words,
            max_case_words=payload.max_case_words,
        )
        return _job_response(
            job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="draft.optimize",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload=job_payload,
            )
        )

    @router.post(
        "/paragraphs/{paragraph_id}/rewrite-jobs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_rewrite(
        project_id: str,
        paragraph_id: str,
        _payload: DraftRewriteRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        job_payload = drafts_service.rewrite_payload(principal, project_id, paragraph_id)
        return _job_response(
            job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="draft.rewrite",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload=job_payload,
            )
        )

    @router.post("/optimization-proposals/{proposal_id}/{decision}")
    def decide_optimization_proposal(
        project_id: str,
        proposal_id: str,
        decision: str,
        payload: DraftRewriteDecisionRequest,
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        return drafts_service.decide_optimization_proposal(
            principal,
            project_id,
            proposal_id,
            decision=decision,
            revision=payload.revision,
            selected_paragraph_ids=payload.selected_paragraph_ids,
        )

    @router.post(
        "/rewrite-candidates/{candidate_id}/accept-jobs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_accept_rewrite(
        project_id: str,
        candidate_id: str,
        payload: DraftRewriteDecisionRequest,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ):
        job_payload = drafts_service.accept_rewrite_payload(
            principal,
            project_id,
            candidate_id,
            revision=payload.revision,
        )
        return _job_response(
            job_service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="draft.accept-rewrite",
                idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                payload=job_payload,
            )
        )

    @router.post("/rewrite-candidates/{candidate_id}/{decision}")
    def decide_rewrite(
        project_id: str,
        candidate_id: str,
        decision: str,
        payload: DraftRewriteDecisionRequest,
        response: Response,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        principal: Principal = Depends(principal_dependency),
    ) -> dict:
        if decision == "accept":
            job_payload = drafts_service.accept_rewrite_payload(
                principal,
                project_id,
                candidate_id,
                revision=payload.revision,
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return _job_response(
                job_service.submit(
                    principal,
                    scope="project",
                    project_id=project_id,
                    job_type="draft.accept-rewrite",
                    idempotency_key=idempotency_key.strip() or str(uuid.uuid4()),
                    payload=job_payload,
                )
            )
        return drafts_service.decide_rewrite(
            principal,
            project_id,
            candidate_id,
            decision=decision,
            revision=payload.revision,
        )

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
