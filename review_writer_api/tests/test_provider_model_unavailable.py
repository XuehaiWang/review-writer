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
