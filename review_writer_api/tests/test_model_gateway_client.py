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

    def test_transient_gateway_failure_is_not_retried_by_client(self) -> None:
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
            side_effect=error,
        ) as open_request:
            with self.assertRaisesRegex(
                model_gateway_client.GatewayRequestError,
                "文本模型服务暂时不可用",
            ) as captured:
                model_gateway_client.call_model("write", label="section")

        self.assertEqual(1, open_request.call_count)
        self.assertEqual(503, captured.exception.status_code)
        self.assertNotIn("HTTP", str(captured.exception))

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
