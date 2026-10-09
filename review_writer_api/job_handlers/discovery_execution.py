"""Discovery task registration independent of HTTP routes."""

from review_writer_api.errors import WorkflowConflict, WorkflowNotFound, WorkflowValidationError
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

    def acquire_handler(context, payload):
        """Acquire Discovery candidates, then refresh their Discovery state.

        Discovery owns this whole chain. It reuses the Library download
        implementation through the registered job handler, so Library never has
        to know that Discovery exists, and no back-channel payload field is
        needed to tell it what to refresh afterwards.
        """

        download = job_service.handlers.get("library.download")
        if download is None:
            raise RuntimeError(
                "The Library download handler is unavailable; cannot acquire Discovery candidates."
            )
        result = dict(download(context, payload) or {})
        principal = Principal(context.user_id, frozenset({Role.USER}))
        project_id = str(payload.get("project_id") or context.project_id or "")
        source_revision = (
            int(payload["discovery_revision"])
            if type(payload.get("discovery_revision")) is int
            else None
        )
        acquisitions = [
            (str(entry.get("candidate_id") or ""), str(entry.get("paper_id") or ""))
            for entry in result.get("results") or []
            if isinstance(entry, dict)
        ]
        acquisitions = [pair for pair in acquisitions if pair[0] and pair[1]]
        refreshed = skipped = 0
        if acquisitions:
            try:
                # One revision for the whole batch: N downloads no longer
                # republish the entire review N times.
                outcome = discovery_service.refresh_acquired_candidates(
                    principal, project_id, acquisitions, source_revision=source_revision
                )
                refreshed = len(outcome.get("refreshed") or [])
                skipped = len(outcome.get("skipped") or [])
            except (WorkflowNotFound, WorkflowConflict, WorkflowValidationError):
                # The papers are already in the Library; a refresh that cannot be
                # published must not undo a completed acquisition.
                skipped = len(acquisitions)
        result["candidate_refreshed_count"] = refreshed
        result["candidate_refresh_skipped_count"] = skipped
        return result

    job_service.register_handler("discovery.acquire", acquire_handler)
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
