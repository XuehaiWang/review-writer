"""Discovery task registration independent of HTTP routes."""

from review_writer_api.security import Principal, Role
from review_writer_api.job_handlers.lifecycle import report_committed_progress


def register_discovery_handlers(discovery_service, job_service, handlers):
    builder = dict(handlers or {}).get("discovery.search")

    def candidate_refresh_handler(context, payload):
        context.report_progress(0, 2)
        principal = Principal(context.user_id, frozenset({Role.USER}))
        context.report_progress(1, 2)
        result = discovery_service.refresh_external_candidate(
            principal,
            str(payload.get("project_id") or context.project_id or ""),
            candidate_id=str(payload.get("candidate_id") or ""),
            paper_id=str(payload.get("paper_id") or ""),
            source_revision=(
                int(payload["source_revision"])
                if type(payload.get("source_revision")) is int
                else None
            ),
        )
        report_committed_progress(context, 2, 2)
        return result

    job_service.register_handler("discovery.candidate-refresh", candidate_refresh_handler)
    if builder is not None:

        def discovery_handler(context, payload):
            principal = Principal(context.user_id, frozenset({Role.USER}))
            discovery_service.validate_search_inputs(principal, str(context.project_id), payload)
            # Discovery exposes stable milestones so the UI can show useful
            # progress even though provider and local-search runtimes vary.
            context.report_progress(0, 6)
            built = builder(context, payload)
            context.report_progress(4, 6)
            context.checkpoint()
            built = discovery_service.enrich_hybrid(
                principal,
                str(context.project_id),
                dict(built or {}),
            )
            context.report_progress(5, 6)
            context.checkpoint()
            result = discovery_service.replace_from_job(
                principal, str(context.project_id), payload, built
            )
            report_committed_progress(context, 6, 6)
            return result

        job_service.register_handler("discovery.search", discovery_handler)
