from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from review_writer_api.job_handlers.model_dispatch import ModelDispatchJobHandlers
from review_writer_api.job_service import JobCancellationRequested, JobYieldRequested
from review_writer_core.model_gateway_client import GatewayRequestError, ModelResultUnknown


def _context(parent_status="queued"):
    parent = SimpleNamespace(project_id="project", job_type="sections.generate",
                             status=parent_status, cancellation_requested=False)
    child = SimpleNamespace(result={})
    repository = SimpleNamespace(get_job=lambda _user, job_id: parent if job_id == "parent" else child)
    return SimpleNamespace(user_id="user", project_id="project", job_id="child",
                           repository=repository, checkpoint=Mock(), report_partial_result=Mock())


def _handler():
    instance = ModelDispatchJobHandlers()
    instance._text_gateway_environment = lambda _context: (
        {"REVIEW_WRITER_MODEL_GATEWAY_URL": "http://gateway/model-responses"},
        {"REVIEW_WRITER_TASK_TOKEN": "leased-token"},
    )
    return instance


def _payload():
    return {"parent_job_id": "parent", "request_key": "key", "stage": "section-source-writing",
            "prompt": "evidence", "response_format": "json"}


def test_model_dispatch_persists_only_verified_provider_output():
    context = _context()
    with patch("review_writer_api.job_handlers.model_dispatch.call_model", return_value="checked") as call:
        result = _handler().model_dispatch(context, _payload())
    assert result == {"request_key": "key", "output_text": "checked"}
    call.assert_called_once_with("evidence", label="section-source-writing", response_format="json",
                                 gateway_url="http://gateway/model-responses", task_token="leased-token")
    context.checkpoint.assert_called_once()


def test_model_dispatch_accepts_a_live_matrix_parent():
    context = _context()
    matrix = SimpleNamespace(project_id="project", job_type="matrix.enrich",
                             status="queued", cancellation_requested=False)
    context.repository.get_job = lambda _user, job_id: matrix if job_id == "parent" else None
    with patch("review_writer_api.job_handlers.model_dispatch.call_model", return_value="facts"):
        assert _handler().model_dispatch(context, _payload())["output_text"] == "facts"


def test_model_dispatch_stops_before_payment_when_parent_is_cancelled():
    with patch("review_writer_api.job_handlers.model_dispatch.call_model") as call:
        with pytest.raises(JobCancellationRequested):
            _handler().model_dispatch(_context(parent_status="cancelled"), _payload())
    call.assert_not_called()


def test_explicit_rate_limit_schedules_bounded_child_retry():
    context = _context()
    failure = GatewayRequestError("busy", status_code=429)
    with patch("review_writer_api.job_handlers.model_dispatch.call_model", side_effect=failure):
        with pytest.raises(JobYieldRequested) as deferred:
            _handler().model_dispatch(context, _payload())
    assert deferred.value.delay_seconds == 30
    context.report_partial_result.assert_called_once_with({"rate_limit_retries": 1})


def test_provider_rate_limit_wrapped_by_gateway_is_still_delayed():
    context = _context()
    failure = GatewayRequestError("busy", status_code=502, code="PROVIDER_RATE_LIMITED",
                                  details={"category": "rate_limited", "provider_status": 429})
    with patch("review_writer_api.job_handlers.model_dispatch.call_model", side_effect=failure):
        with pytest.raises(JobYieldRequested) as deferred:
            _handler().model_dispatch(context, _payload())
    assert deferred.value.queue_reason == "provider_rate_limit"
    assert deferred.value.delay_seconds == 30


def test_rate_limit_exhausts_local_retries_without_another_yield():
    context = _context()
    context.repository.get_job = lambda _user, job_id: (
        SimpleNamespace(project_id="project", job_type="matrix.enrich", status="queued",
                        cancellation_requested=False) if job_id == "parent"
        else SimpleNamespace(result={"rate_limit_retries": 2}))
    failure = GatewayRequestError("busy", status_code=502,
                                  details={"category": "rate_limited", "provider_status": 429})
    with patch("review_writer_api.job_handlers.model_dispatch.call_model", side_effect=failure):
        with pytest.raises(GatewayRequestError):
            _handler().model_dispatch(context, _payload())
    context.report_partial_result.assert_not_called()


def test_unknown_provider_outcome_stops_without_replaying_model_call():
    from review_writer_api.scientific_runner import ScientificRunFailed
    with patch("review_writer_api.job_handlers.model_dispatch.call_model",
               side_effect=ModelResultUnknown("uncertain")) as call:
        with pytest.raises(ScientificRunFailed) as failed:
            _handler().model_dispatch(_context(), _payload())
    assert failed.value.retryable is False
    call.assert_called_once()
