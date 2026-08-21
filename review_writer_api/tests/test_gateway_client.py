from __future__ import annotations

import io
import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from review_writer_api.gateway_client import GatewayTaskEnvironmentClient


class _Response:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class GatewayTaskEnvironmentClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = SimpleNamespace(
            job_id="00000000-0000-0000-0000-000000000001",
            lease_token="00000000-0000-0000-0000-000000000002",
            lease_generation=3,
        )

    def test_token_exchange_waits_through_transient_gateway_restart(self) -> None:
        calls = 0

        def open_request(_request, timeout):
            nonlocal calls
            calls += 1
            self.assertEqual(2.0, timeout)
            if calls < 3:
                raise urllib.error.URLError("gateway restarting")
            return _Response({"task_token": "short-lived-token"})

        client = GatewayTaskEnvironmentClient(
            "http://model-gateway:8782/api/internal/v1/model-responses",
            "worker-secret",
            timeout=2,
            attempts=5,
        )
        with patch(
            "review_writer_api.gateway_client.urllib.request.urlopen",
            side_effect=open_request,
        ), patch("review_writer_api.gateway_client.time.sleep") as sleep:
            normal, secrets = client.environment_for_job(self.context)

        self.assertEqual(3, calls)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual("short-lived-token", secrets["REVIEW_WRITER_TASK_TOKEN"])
        self.assertNotIn("API_KEY", " ".join(normal))

    def test_non_retryable_authentication_failure_is_not_replayed(self) -> None:
        client = GatewayTaskEnvironmentClient(
            "http://model-gateway:8782/api/internal/v1/model-responses",
            "wrong-worker-secret",
            attempts=5,
        )
        error = urllib.error.HTTPError(
            client.token_endpoint,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"{}"),
        )
        self.addCleanup(error.close)
        with patch(
            "review_writer_api.gateway_client.urllib.request.urlopen",
            side_effect=error,
        ) as open_request, patch(
            "review_writer_api.gateway_client.time.sleep"
        ) as sleep:
            with self.assertRaises(RuntimeError):
                client.environment_for_job(self.context)

        self.assertEqual(1, open_request.call_count)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
