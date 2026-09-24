"""A separately leased, durable text-model execution unit."""

from __future__ import annotations

from review_writer_api.job_service import JobCancellationRequested, JobYieldRequested
from review_writer_api.job_queues import DELEGATED_TEXT_PARENT_JOB_TYPES
from review_writer_api.scientific_runner import ScientificInsufficientCredit, ScientificRunFailed
from review_writer_core.model_gateway_client import GatewayRequestError, ModelResultUnknown, call_model


class ModelDispatchJobHandlers:
    def model_dispatch(self, context, payload):
        parent_id = str(payload.get("parent_job_id") or "")
        parent = context.repository.get_job(context.user_id, parent_id)
        if (
            parent is None or parent.project_id != context.project_id
            or parent.job_type not in DELEGATED_TEXT_PARENT_JOB_TYPES
            or parent.status not in {"queued", "running"}
            or parent.cancellation_requested
        ):
            raise JobCancellationRequested()
        normal, secrets = self._text_gateway_environment(context)
        try:
            text = call_model(
                str(payload["prompt"]), label=str(payload["stage"]),
                response_format=str(payload["response_format"]),
                gateway_url=str(normal["REVIEW_WRITER_MODEL_GATEWAY_URL"]),
                task_token=str(secrets["REVIEW_WRITER_TASK_TOKEN"]),
            )
        except GatewayRequestError as exc:
            if exc.status_code == 402:
                raise ScientificInsufficientCredit(attempts=1) from exc
            current = context.repository.get_job(context.user_id, context.job_id)
            retries = int((current.result or {}).get("rate_limit_retries") or 0) if current else 0
            rate_limited = (exc.status_code == 429 or exc.details.get("category") == "rate_limited"
                            or exc.details.get("provider_status") == 429)
            if rate_limited and retries < 2:
                context.report_partial_result({"rate_limit_retries": retries + 1})
                raise JobYieldRequested(delay_seconds=30 * (retries + 1), queue_reason="provider_rate_limit") from exc
            if exc.status_code == 409:
                raise ScientificRunFailed(
                    "The original model request may still be running. It was not submitted again; check its outcome before retrying.",
                    attempts=1, retryable=False,
                    details={"model_outcome": "unknown"},
                ) from exc
            raise
        except ModelResultUnknown as exc:
            raise ScientificRunFailed(
                "The original model request outcome is unknown. It was not sent again; check the provider result before retrying.",
                attempts=1, retryable=False, details={"model_outcome": "unknown"},
            ) from exc
        context.checkpoint()
        return {"request_key": str(payload["request_key"]), "output_text": text}
