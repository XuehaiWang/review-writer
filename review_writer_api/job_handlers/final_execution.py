"""Final task registration independent of HTTP routes."""

from review_writer_api.security import Principal, Role
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
        context.report_progress(1, 4)
        final_service.validate_task_inputs(principal, str(context.project_id), payload)
        context.checkpoint()
        context.report_progress(2, 4)
        build_revision = payload["expected_revision"]
        context.checkpoint()
        result = final_service.build(principal, str(context.project_id), expected_revision=build_revision)
        report_committed_progress(context, 4, 4)
        return result

    job_service.register_handler("final.build", build_handler)
