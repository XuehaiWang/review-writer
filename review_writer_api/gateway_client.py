"""Worker-side client for exchanging a PostgreSQL lease for model access."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from review_writer_api.credentials import ProviderSettingsError


class GatewayTaskEnvironmentClient:
    """Provides the small interface consumed by native workflow handlers."""

    def __init__(
        self,
        model_endpoint: str,
        worker_token: str,
        *,
        timeout: float = 15.0,
        attempts: int = 5,
    ):
        self.model_endpoint = str(model_endpoint or "").strip().rstrip("/")
        self.worker_token = str(worker_token or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.attempts = max(1, min(int(attempts), 8))
        if not self.model_endpoint:
            raise ValueError("The internal model gateway endpoint is required.")
        if not self.worker_token:
            raise ValueError("The internal worker token is required.")

    @property
    def token_endpoint(self) -> str:
        return self.model_endpoint.rsplit("/", 1)[0] + "/task-token"

    def environment_for_job(self, context: Any) -> tuple[dict[str, str], dict[str, str]]:
        payload = json.dumps(
            {
                "job_id": context.job_id,
                "lease_token": context.lease_token,
                "lease_generation": context.lease_generation,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                self.token_endpoint,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Review-Writer-Worker-Token": self.worker_token,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("Gateway token response is not an object.")
                result = decoded
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last_error = exc
            if attempt < self.attempts:
                time.sleep(min(5.0, float(2 ** (attempt - 1))))
        if result is None:
            raise RuntimeError(
                "The internal model gateway could not issue a task token after bounded retries."
            ) from last_error
        task_token = str(result.get("task_token") or "")
        if not task_token:
            raise RuntimeError("The internal model gateway returned an empty task token.")
        return (
            {
                "REVIEW_WRITER_MODEL_GATEWAY_URL": self.model_endpoint,
                "REVIEW_WRITER_IMAGE_GATEWAY_URL": (
                    self.model_endpoint.rsplit("/", 1)[0] + "/image-generations"
                ),
            },
            {"REVIEW_WRITER_TASK_TOKEN": task_token},
        )


def test_provider_through_gateway(
    model_endpoint: str,
    service_token: str,
    *,
    provider_kind: str,
    actor_user_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    endpoint = str(model_endpoint or "").rstrip("/").rsplit("/", 1)[0]
    request = urllib.request.Request(
        endpoint + "/provider-test",
        data=json.dumps(
            {"provider_kind": provider_kind, "actor_user_id": actor_user_id},
            separators=(",", ":"),
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Review-Writer-Worker-Token": str(service_token or ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("detail") or "").strip()
        except (ValueError, UnicodeError):
            detail = ""
        raise ProviderSettingsError(
            detail or "The internal model gateway provider test was rejected."
        ) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ProviderSettingsError(
            "The internal model gateway provider test is temporarily unavailable."
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("The internal model gateway returned an invalid provider test.")
    return result
