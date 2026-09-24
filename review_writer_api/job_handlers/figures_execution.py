"""Figures task registration independent of HTTP routes."""

from typing import Any
from review_writer_api.security import Principal, Role
from review_writer_api.domain_services.figures import FigureSafetyBlocked, SAFETY_ERROR
from review_writer_api.errors import WorkflowError
from review_writer_api.job_service import (
    JobCancellationRequested, JobShutdownRequested, JobYieldRequested,
)
from review_writer_api.job_handlers.lifecycle import report_committed_progress


def register_figure_handlers(figures_service, job_service, handlers):
    builder = dict(handlers or {}).get("figures.redraw")
    if builder is not None:

        def redraw_handler(context, payload):
            figure_ids = list(payload.get("figure_ids") or [])
            execution_payload = {**payload, "producer_job_id": context.job_id}
            previous = context.repository.get_job(context.user_id, context.job_id)
            checkpoint = dict(previous.result or {}) if previous else {}
            requested = set(figure_ids)
            results: list[dict[str, Any]] = [
                dict(row) for row in checkpoint.get("outputs") or []
                if isinstance(row, dict) and str(row.get("figure_id") or "") in requested
            ]
            errors: list[dict[str, Any]] = [
                dict(row) for row in checkpoint.get("errors") or []
                if isinstance(row, dict) and str(row.get("figure_id") or "") in requested
            ]
            completed = {
                str(row["figure_id"]) for row in [*results, *errors]
                if row.get("figure_id")
            }
            context.report_progress(len(completed), len(figure_ids))
            principal = Principal(context.user_id, frozenset({Role.USER}))
            for index, figure_id in enumerate(figure_ids, start=1):
                if figure_id in completed:
                    continue
                context.checkpoint()
                item = figures_service.resolve_redraw_item(
                    principal,
                    str(context.project_id),
                    execution_payload,
                    str(figure_id),
                )
                try:
                    built = builder(context, item)
                    context.checkpoint()
                    result = figures_service.publish_redraw(
                        principal,
                        str(context.project_id),
                        execution_payload,
                        built,
                    )
                except (JobCancellationRequested, JobShutdownRequested):
                    raise
                except Exception as exc:
                    if SAFETY_ERROR.search(str(exc)):
                        normalized: Exception = FigureSafetyBlocked(
                            "The image provider blocked this chemistry figure during safety review. No output was admitted."
                        )
                    else:
                        normalized = exc
                    errors.append(
                        {
                            "figure_id": str(figure_id),
                            "error_code": (
                                normalized.code
                                if isinstance(normalized, WorkflowError)
                                else "FIGURE_REDRAW_FAILED"
                            ),
                            "error": str(normalized),
                        }
                    )
                    context.report_partial_result(
                        {
                            "figure_count": len(results),
                            "figure_ids": figure_ids,
                            "outputs": results,
                            "errors": errors,
                        }
                    )
                    if len(figure_ids) == 1:
                        if normalized is exc:
                            raise
                        raise normalized from exc
                else:
                    results.append(result)
                    context.report_partial_result(
                        {
                            "figure_count": len(results),
                            "figure_ids": figure_ids,
                            "outputs": results,
                            "errors": errors,
                        }
                    )
                report_committed_progress(context, index, len(figure_ids))
                # The old batch loop held its image worker through every
                # figure. Yield only when a different queued image task can
                # run; completed images remain checkpointed and are skipped
                # when this batch is claimed again.
                if index < len(figure_ids) and context.repository.has_queued_job("image"):
                    raise JobYieldRequested(queue_reason="image_batch_yield")
            return {
                "figure_count": len(results),
                "figure_ids": figure_ids,
                "outputs": results,
                "errors": errors,
            }

        job_service.register_handler("figures.redraw", redraw_handler)
