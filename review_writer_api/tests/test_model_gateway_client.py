from __future__ import annotations

import json
import os
import unittest
import urllib.error
from unittest import mock

from review_writer_core import model_gateway_client


class _Response:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        return None


class ModelGatewayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ,
            {
                "REVIEW_WRITER_MODEL_GATEWAY_URL": "http://127.0.0.1:8770/api/internal/v1/model-responses",
                "REVIEW_WRITER_TASK_TOKEN": "task-token",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_text_request_uses_task_token_and_deterministic_key(self) -> None:
        requests = []

        def open_request(request, **_kwargs):
            requests.append(request)
            return _Response({"output_text": "draft text"})

        with mock.patch.object(model_gateway_client.urllib.request, "urlopen", open_request):
            first = model_gateway_client.call_model("write", label="section", response_format="text")
            second = model_gateway_client.call_model("write", label="section", response_format="text")

        self.assertEqual("draft text", first)
        self.assertEqual("draft text", second)
        first_body = json.loads(requests[0].data.decode("utf-8"))
        second_body = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual("text", first_body["response_format"])
        self.assertEqual(first_body["request_key"], second_body["request_key"])
        self.assertEqual("Bearer task-token", requests[0].get_header("Authorization"))

    def test_json_request_removes_fence_and_returns_object(self) -> None:
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            return_value=_Response({"output_text": "```json\n{\"ok\": true}\n```"}),
        ):
            result = model_gateway_client.call_json_model("plan", label="topic")

        self.assertEqual({"ok": True}, result)

    def test_optional_request_does_not_poll_or_replay_after_timeout(self) -> None:
        with (
            mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=TimeoutError("slow")) as opened,
            mock.patch.object(model_gateway_client, "_recover_model_result") as recover,
        ):
            with self.assertRaises(TimeoutError):
                model_gateway_client.call_json_model(
                    "resolve acronym", label="discovery-query-concepts",
                    timeout_seconds=20, recover_on_timeout=False,
                )
        opened.assert_called_once()
        recover.assert_not_called()

    def test_optional_request_does_not_recover_proxy_failure(self) -> None:
        import io
        error = urllib.error.HTTPError("http://gateway", 503, "busy", {}, io.BytesIO(b'{}'))
        with (
            mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=error) as opened,
            mock.patch.object(model_gateway_client, "_recover_model_result") as recover,
        ):
            with self.assertRaises(model_gateway_client.GatewayRequestError):
                model_gateway_client.call_model("resolve", label="concepts", recover_on_timeout=False)
        opened.assert_called_once()
        recover.assert_not_called()
        error.close()

    def test_json_request_accepts_trailing_prose_and_a_second_object(self) -> None:
        output = (
            '{"paragraphs": [{"paragraph_id": "S10-p1"}]}'
            '\nThe requested section is complete.\n'
            '{"provider_debug": "ignored"}'
        )
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            return_value=_Response({"output_text": output}),
        ):
            result = model_gateway_client.call_json_model(
                "plan",
                label="section",
                required_list="paragraphs",
            )

        self.assertEqual([{"paragraph_id": "S10-p1"}], result["paragraphs"])
        self.assertNotIn("provider_debug", result)

    def test_json_request_selects_candidate_matching_required_contract(self) -> None:
        output = (
            '{"provider_note": "first object is not the answer"}\n'
            '```json\n{"paragraphs": [], "overview": "bounded"}\n```'
        )
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            return_value=_Response({"output_text": output}),
        ):
            result = model_gateway_client.call_json_model(
                "plan",
                label="section",
                required_list="paragraphs",
            )

        self.assertEqual("bounded", result["overview"])

    def test_json_request_repairs_invalid_latex_backslash_escape(self) -> None:
        output = (
            "<think>Internal reasoning with {draft notes}.</think>\n"
            r'{"score": 95, "excerpt": "$25^{\\\mathrm{C}}$"}'
        )
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            return_value=_Response({"output_text": output}),
        ):
            result = model_gateway_client.call_json_model("score", label="draft")

        self.assertEqual(95, result["score"])
        self.assertEqual(r"$25^{\\mathrm{C}}$", result["excerpt"])

    def test_json_request_reports_missing_contract_without_leaking_decoder_error(self) -> None:
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            return_value=_Response({"output_text": '{"provider_note": "no answer"}'}),
        ):
            with self.assertRaisesRegex(RuntimeError, "required `paragraphs` list"):
                model_gateway_client.call_json_model(
                    "plan",
                    label="section",
                    required_list="paragraphs",
                )

    def test_missing_task_credentials_are_rejected(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "REVIEW_WRITER_MODEL_GATEWAY_URL": "",
                "REVIEW_WRITER_TASK_TOKEN": "",
            },
            clear=False,
        ):
            self.assertFalse(model_gateway_client.gateway_configured())
            with self.assertRaisesRegex(RuntimeError, "configuration is incomplete"):
                model_gateway_client.call_model("plan", label="topic")

    def test_failed_gateway_request_is_checked_but_not_reposted(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8770/internal",
            503,
            "unavailable",
            {},
            _Response({"error": "provider exhausted retries"}),
        )
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            side_effect=[error, _Response({"status": "failed"})],
        ) as open_request:
            with self.assertRaisesRegex(
                model_gateway_client.GatewayRequestError,
                "文本模型服务暂时不可用",
            ) as captured:
                model_gateway_client.call_model("write", label="section")

        self.assertEqual(["POST", "GET"], [call.args[0].get_method() for call in open_request.call_args_list])
        self.assertEqual(503, captured.exception.status_code)
        self.assertNotIn("HTTP", str(captured.exception))

    def test_slow_fact_extraction_and_verification_recover_without_reposting(self) -> None:
        for label, field, duration in [("matrix-facts-paper", "facts", 660),
                                        ("fact-verify-paper", "verdicts", 398)]:
            with self.subTest(label=label):
                now = [0.0]
                requests = []
                result = {field: [{"fact_id": "F1"}]}

                def open_request(request, **kwargs):
                    requests.append(request)
                    if request.get_method() == "POST":
                        now[0] += kwargs["timeout"]
                        raise TimeoutError("timed out")
                    self.assertEqual("Bearer task-token", request.get_header("Authorization"))
                    self.assertIsNone(request.data)
                    original_key = json.loads(requests[0].data)["request_key"]
                    self.assertTrue(request.full_url.endswith("/" + original_key))
                    return _Response({"status": "running"} if now[0] < duration else {
                        "status": "succeeded", "result": {"output_text": json.dumps(result)}})

                with (
                    mock.patch.object(model_gateway_client.urllib.request, "urlopen", open_request),
                    mock.patch.object(model_gateway_client.time, "monotonic", side_effect=lambda: now[0]),
                    mock.patch.object(model_gateway_client.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
                ):
                    recovered = model_gateway_client.call_json_model("facts", label=label, required_list=field)
                self.assertEqual(result, recovered)
                self.assertEqual(1, sum(r.get_method() == "POST" for r in requests))

    def test_recovery_failed_request_is_not_replayed(self) -> None:
        with mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=[
            TimeoutError("timed out"), _Response({"status": "failed"}),
        ]) as opened:
            with self.assertRaises(model_gateway_client.GatewayRequestError) as error:
                model_gateway_client.call_model("facts", label="matrix")
        self.assertEqual("MODEL_REQUEST_FAILED", error.exception.code)
        self.assertEqual(["POST", "GET"], [c.args[0].get_method() for c in opened.call_args_list])

    def test_recovery_stops_on_cancelled_expired_or_missing_request(self) -> None:
        for status in (401, 403, 404, 409):
            with self.subTest(status=status), mock.patch.object(
                model_gateway_client.urllib.request, "urlopen", side_effect=[
                    TimeoutError("timed out"),
                    urllib.error.HTTPError("http://gateway/result", status, "stopped", {}, _Response({})),
                ],
            ) as opened:
                with self.assertRaises(model_gateway_client.GatewayRequestError) as error:
                    model_gateway_client.call_model("facts", label="matrix")
                self.assertEqual(status, error.exception.status_code)
                self.assertEqual(2, opened.call_count)

    def test_recovery_has_a_deadline_when_gateway_never_finishes(self) -> None:
        now = [0.0]
        def open_request(request, **kwargs):
            if request.get_method() == "POST":
                raise TimeoutError("timed out")
            return _Response({"status": "running"})
        with (
            mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=open_request) as opened,
            mock.patch.object(model_gateway_client, "MODEL_RESULT_RECOVERY_SECONDS", 10),
            mock.patch.object(model_gateway_client.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(model_gateway_client.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
        ):
            with self.assertRaisesRegex(RuntimeError, "recovery timed out"):
                model_gateway_client.call_model("facts", label="matrix")
        self.assertEqual(10, now[0])
        self.assertEqual(3, opened.call_count)

    def test_recovery_survives_a_temporary_status_connection_failure(self) -> None:
        with (
            mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=[
                urllib.error.URLError("connection reset"),
                TimeoutError("status timeout"),
                _Response({"status": "succeeded", "result": {"output_text": "recovered"}}),
            ]) as opened,
            mock.patch.object(model_gateway_client.time, "sleep"),
        ):
            self.assertEqual("recovered", model_gateway_client.call_model("facts", label="matrix"))
        self.assertEqual(["POST", "GET", "GET"], [c.args[0].get_method() for c in opened.call_args_list])

    def test_insufficient_credit_exposes_only_user_facing_message(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8770/internal",
            402,
            "payment required",
            {},
            _Response(
                {
                    "error": {
                        "code": "INSUFFICIENT_CREDIT",
                        "message": "余额不足，无法开始本次外部模型调用。",
                        "details": {"required_usd": "0.003", "available_usd": "0"},
                    }
                }
            ),
        )
        with mock.patch.object(
            model_gateway_client.urllib.request,
            "urlopen",
            side_effect=error,
        ):
            with self.assertRaises(model_gateway_client.GatewayRequestError) as captured:
                model_gateway_client.call_model("write", label="section")

        self.assertEqual("INSUFFICIENT_CREDIT", captured.exception.code)
        self.assertIn("余额不足", str(captured.exception))
        self.assertNotIn("402", str(captured.exception))
        self.assertNotIn("required_usd", str(captured.exception))

    def test_proxy_503_waits_for_original_request_without_reposting(self):
        with (
            mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=[
                urllib.error.HTTPError("http://gateway", 503, "proxy", {}, _Response({})),
                _Response({"status": "running"}),
                _Response({"status": "succeeded", "result": {"output_text": "recovered"}}),
            ]) as opened,
            mock.patch.object(model_gateway_client.time, "sleep"),
        ):
            self.assertEqual("recovered", model_gateway_client.call_model("write", label="draft"))
        self.assertEqual(["POST", "GET", "GET"], [c.args[0].get_method() for c in opened.call_args_list])

    def test_terminal_503_stops_waiting_and_preserves_provider_reason(self):
        with (
            mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=[
                urllib.error.HTTPError("http://gateway", 503, "failed", {}, _Response({"detail": "session quota exhausted"})),
                _Response({"status": "failed"}),
            ]) as opened,
            mock.patch.object(model_gateway_client.time, "sleep") as sleep,
        ):
            with self.assertRaises(model_gateway_client.GatewayRequestError) as error:
                model_gateway_client.call_model("write", label="draft")
        self.assertEqual("session quota exhausted", error.exception.details["provider_message"])
        self.assertEqual("quota_exhausted", error.exception.details["category"])
        self.assertEqual(2, opened.call_count)
        sleep.assert_not_called()

    def test_fastapi_structured_error_detail_reaches_feedback_client(self):
        response = _Response({"detail": {"code": "PROVIDER_CONTEXT_LIMIT", "message": "Provider-specific message",
            "details": {"provider_code": "context_length_exceeded", "provider_status": 503}}})
        error = urllib.error.HTTPError("http://gateway", 502, "failed", {}, response)
        with mock.patch.object(model_gateway_client.urllib.request, "urlopen", side_effect=[error, _Response({"status": "failed"})]):
            with self.assertRaises(model_gateway_client.GatewayRequestError) as failure:
                model_gateway_client.call_json_model("write", label="draft")
        self.assertEqual("PROVIDER_CONTEXT_LIMIT", failure.exception.code)
        self.assertEqual("context_limit", failure.exception.details["category"])
        self.assertEqual(503, failure.exception.details["provider_status"])

    def test_image_request_uses_image_gateway_and_returns_binary(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"REVIEW_WRITER_IMAGE_GATEWAY_URL": "http://127.0.0.1:8770/image"},
                clear=False,
            ),
            mock.patch.object(
                model_gateway_client.urllib.request,
                "urlopen",
                return_value=_Response(
                    {
                        "request_id": "image-request",
                        "image_base64": "aW1hZ2UtYnl0ZXM=",
                        "image_mime_type": "image/png",
                    }
                ),
            ) as open_request,
        ):
            image_bytes, metadata = model_gateway_client.call_image_model(
                "edit it",
                label="figure-redraw",
                images=[("image/png", b"source")],
            )

        self.assertEqual(b"image-bytes", image_bytes)
        self.assertEqual("image-request", metadata["request_id"])
        request = open_request.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("edit", body["operation"])
        self.assertEqual("c291cmNl", body["images"][0]["data_base64"])
        self.assertEqual("Bearer task-token", request.get_header("Authorization"))
