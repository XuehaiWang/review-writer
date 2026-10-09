"""Offline parity checks: diagnostics must not silently opt into JSON mode."""
import json
from types import SimpleNamespace
import unittest

from review_writer_api.model_gateway import ModelGatewayService, httpx, text_provider_request
from review_writer_core.model_gateway_client import structured_json_prompt


class RequestContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_gateway_uses_shared_request_without_extra_options(self):
        for wire in ("chat-completions", "responses"):
            for output_format in ("json", "text"):
                runtime = SimpleNamespace(enabled=True, base_url="https://fixture.invalid/v1",
                                          api_key="test-only", wire_api=wire)
                tier = SimpleNamespace(model="test-model", wire_api="")
                prompt = structured_json_prompt("中文科学问题", {"type": "object"})
                endpoint, payload, headers, resolved = text_provider_request(
                    tier=tier, runtime=runtime, prompt=prompt,
                    response_format=output_format, idempotency_key="isolated-key")
                captured = []
                def provider(request):
                    captured.append(request)
                    return httpx.Response(200, json={"output_text": "{}"})
                gateway = object.__new__(ModelGatewayService)
                gateway._text_runtime = lambda _: runtime
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                    gateway._provider_client = client
                    await gateway._provider_call(tier=tier, prompt=prompt,
                                                response_format=output_format, idempotency_key="isolated-key")
                self.assertEqual(1, len(captured))
                self.assertEqual(endpoint, str(captured[0].url))
                self.assertEqual(payload, json.loads(captured[0].content))
                self.assertEqual(resolved, wire)
                for key, value in headers.items():
                    self.assertEqual(value, captured[0].headers[key])
                for key in ("response_format", "max_tokens", "max_output_tokens", "thinking", "reasoning_effort"):
                    self.assertNotIn(key, payload)
                if wire == "chat-completions":
                    self.assertEqual(prompt + ("\nReturn only one valid JSON object." if output_format == "json" else ""),
                                     payload["messages"][0]["content"])
                else:
                    self.assertEqual(prompt, payload["input"][0]["content"])

    def test_tier_wire_overrides_connection_without_mutating_inputs(self):
        runtime = SimpleNamespace(base_url="https://fixture.invalid/v1", api_key="test", wire_api="responses")
        tier = SimpleNamespace(model="m", wire_api="chat-completions")
        endpoint, payload, _, wire = text_provider_request(
            tier=tier, runtime=runtime, prompt="p", idempotency_key="k")
        self.assertTrue(endpoint.endswith("/chat/completions"))
        self.assertEqual("chat-completions", wire)
        self.assertEqual("responses", runtime.wire_api)
        self.assertNotIn("response_format", payload)
