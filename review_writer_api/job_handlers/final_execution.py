"""Final task registration independent of HTTP routes."""

from review_writer_api.security import Principal, Role
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_handlers.lifecycle import report_committed_progress, register_publishing_handler


def register_final_handlers(final_service, job_service, handlers):
    available = dict(handlers or {})

    def register(job_type, publisher, *, progress_total):
        register_publishing_handler(
            job_service, job_type, available.get(job_type), publisher,
            progress_total=progress_total, validate=final_service.validate_task_inputs,
        )

    register("final.conclusion", final_service.publish_conclusion, progress_total=3)
    register("final.overview", final_service.publish_overview, progress_total=4)
    register("final.export", final_service.publish_export, progress_total=3)
    register("final.pdf", final_service.publish_pdf, progress_total=5)

    def build_handler(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        builder = available.get("final.build")
        context.report_progress(1, 4)
        final_service.validate_task_inputs(principal, str(context.project_id), payload)
        current = final_service.build_payload(principal, str(context.project_id))
        context.checkpoint()
        context.report_progress(2, 4)
        generated: dict = {}
        generation_error = ""
        if current.get("generation_fields") and builder is not None:
            try:
                generated = dict(builder(context, current) or {})
            except Exception as exc:
                if context.cancellation_requested():
                    raise
                # Missing auto front matter is a publication warning, not a
                # reason to discard an otherwise valid final manuscript.
                generation_error = f"{type(exc).__name__}: {exc}"
        try:
            front_result = final_service.publish_generated_front_matter(
                principal, str(context.project_id), current, generated,
                generation_error=generation_error,
            )
            build_revision = front_result.get("revision", current["expected_revision"])
        except WorkflowConflict:
            build_revision = current["expected_revision"]
        context.checkpoint()
        result = final_service.build(principal, str(context.project_id), expected_revision=build_revision)
        report_committed_progress(context, 4, 4)
        return result

    job_service.register_handler("final.build", build_handler)
