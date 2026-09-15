"""Shared build/publish lifecycle for versioned workflow jobs."""

from review_writer_api.security import Principal, Role


def report_committed_progress(context, current, total):
    """Do not cancel a committed publication; still fence stale workers."""
    return context.repository.update_job_progress(
        context.job_id, current, total,
        lease_token=context.lease_token,
        lease_generation=context.lease_generation,
    )


def register_publishing_handler(job_service, job_type, builder, publisher, *,
                                progress_total, validate=None, after_publish=None, dispatch=None):
    if builder is None:
        return

    def handler(context, payload):
        principal = Principal(context.user_id, frozenset({Role.USER}))
        project_id = str(context.project_id)
        context.report_progress(0, progress_total)
        if validate is not None:
            validate(principal, project_id, payload)
        built = builder(context, payload)
        context.checkpoint()
        context.report_progress(progress_total - 1, progress_total)
        result = publisher(principal, project_id, payload, built)
        if after_publish is not None:
            result = after_publish(context, principal, project_id, payload, result)
        report_committed_progress(context, progress_total, progress_total)
        return result

    job_service.register_handler(job_type, dispatch(handler) if dispatch else handler)
