import io
import json
from urllib.error import HTTPError

from review_writer_api.model_gateway import GatewayProviderError
from review_writer_api.scientific_entrypoint import _error_category
from review_writer_core.model_gateway_client import _gateway_http_error
from review_writer_core.provider_errors import normalize_provider_error


def test_unavailable_model_is_actionable_and_not_retryable_across_gateway():
    failure = normalize_provider_error(404, {"error": {
        "type": "not_found_error", "message": 'Model "any-model" is not available for this group'
    }})
    assert failure["category"] == "model_unavailable"
    gateway = GatewayProviderError("Provider rejected this model", provider_error=failure)
    assert gateway.status_code == 422
    assert _error_category(gateway.status_code) == "validation"
    body = io.BytesIO(json.dumps({"detail": gateway.gateway_detail}).encode())
    error = _gateway_http_error(HTTPError("http://gateway", 422, "failed", {}, body))
    assert error.details["category"] == "model_unavailable"
    assert "服务账号" in str(error)


def test_generic_missing_endpoint_and_outage_are_not_model_access_errors():
    assert normalize_provider_error(404, "Endpoint not found")["category"] == "unknown"
    assert normalize_provider_error(503, "Model temporarily unavailable")["category"] == "transient"


def test_timeout_has_concise_public_message_without_reclassifying_other_failures():
    from review_writer_core.provider_errors import public_model_error, MODEL_TIMEOUT_MESSAGE
    for message in ("Scientific provider timed out (HTTP 524) after 3 attempts. Please retry the task.",
                    "Scientific task failed: Model provider returned HTTP 524: Proxy Read Timeout",
                    "Model request status_code=524"):
        assert public_model_error(message) == MODEL_TIMEOUT_MESSAGE
    for message in ("PDF compilation timed out", "Model is not available", "request id: 20260915524"):
        assert public_model_error(message) == message
    gateway = GatewayProviderError("Upstream timeout", provider_error=normalize_provider_error(524, "Proxy Read Timeout"))
    body = io.BytesIO(json.dumps({"detail": gateway.gateway_detail}).encode())
    error = _gateway_http_error(HTTPError("http://gateway", 502, "failed", {}, body))
    assert str(error) == MODEL_TIMEOUT_MESSAGE
    assert error.details["provider_status"] == 524


def test_job_presentation_preserves_diagnostics_checkpoint_and_retry_action():
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from review_writer_api.job_service import job_payload
    from review_writer_core.provider_errors import MODEL_TIMEOUT_MESSAGE
    raw = "Scientific provider timed out (HTTP 524) after 3 attempts."
    saved = {"section_checkpoint": {"entries": {"S01": {"status": "completed"}}}}
    job = SimpleNamespace(id="job", project_id="project", scope="project", status="failed",
        job_type="sections.generate", result=saved, progress_current=1, progress_total=2,
        cancellation_requested=False, error_code="SCIENTIFIC_RUN_FAILED", error_message=raw,
        retry_of_job_id=None, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        started_at=None, finished_at=None)
    result = job_payload(job)
    assert result["error_message"] == MODEL_TIMEOUT_MESSAGE
    assert result["available_actions"] == ["retry"]
    assert result["result"] == saved
    assert job.error_message == raw
