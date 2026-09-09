"""Draft task registration independent of HTTP route construction."""

from review_writer_api.job_handlers.lifecycle import register_publishing_handler


def register_draft_handlers(drafts_service, job_service, handlers):
    available = dict(handlers or {})

    def accept_rewrite(context, payload):
        stored = payload.get("candidate_evaluation")
        if isinstance(stored, dict) and stored.get("evaluation_scope") == "single_paragraph":
            return dict(stored)
        return available["draft.accept-rewrite"](context, payload)

    def apply_optimization(context, principal, project_id, payload, result):
        if (bool(payload.get("auto_apply_safe", True))
                and result.get("proposal_created") and result.get("proposal_id")):
            automatic = drafts_service.auto_apply_optimization_proposal(
                principal, project_id, str(result["proposal_id"]),
                revision=int(result["revision"]),
            )
            result = {**result, **automatic}
            if (automatic.get("auto_applied") is False
                    and automatic.get("auto_apply_status") == "manual_review_required"):
                result["repair_status"] = "requires_user_input"
        return result

    for job_type, publisher, total in (
        ("draft.evaluate", drafts_service.publish_evaluation, 3),
        ("draft.rewrite", drafts_service.publish_rewrite_candidate, 4),
        ("draft.accept-rewrite", drafts_service.publish_accepted_rewrite, 2),
        ("draft.optimize", drafts_service.publish_optimization, 5),
    ):
        builder = available.get(job_type)
        if builder is not None and job_type == "draft.accept-rewrite":
            builder = accept_rewrite
        register_publishing_handler(
            job_service, job_type, builder, publisher, progress_total=total,
            validate=drafts_service.validate_task_inputs,
            after_publish=apply_optimization if job_type == "draft.optimize" else None,
        )
