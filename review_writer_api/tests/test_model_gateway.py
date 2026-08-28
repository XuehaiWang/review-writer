from __future__ import annotations

import base64
import asyncio
import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx2 as httpx

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.billing import BillingService
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User, database_session, utc_now
from review_writer_api.model_catalog import resolve_model_tier
from review_writer_api.model_gateway import (
    GatewaySafetyBlocked,
    InvalidTaskToken,
    ModelGatewayService,
    calculate_provider_cost,
)
from review_writer_api.job_queues import queue_for_job_type
from review_writer_api.repositories import HostedProjectRepository
from review_writer_api.workflow_models import WorkflowJob


TEST_KEY = base64.urlsafe_b64encode(b"g" * 32).decode("ascii")


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.job_id = uuid.uuid4()
        self.image_job_id = uuid.uuid4()
        self.embedding_job_id = uuid.uuid4()
        with database_session(self.sessions) as session:
            session.add(
                User(
                    id=self.user_id,
                    email="gateway@example.com",
                    password_hash="test",
                )
            )
            session.add(
                Project(
                    id=self.project_id,
                    user_id=self.user_id,
                    slug="gateway-project",
                    model_tier="terra",
                )
            )
            session.add(
                WorkflowJob(
                    id=self.job_id,
                    user_id=self.user_id,
                    project_id=self.project_id,
                    scope="project",
                    job_type="draft.evaluate",
                    status="running",
                    idempotency_scope_key=str(self.project_id),
                    idempotency_key="gateway-test",
                )
            )
            session.add(
                WorkflowJob(
                    id=self.embedding_job_id,
                    user_id=self.user_id,
                    project_id=self.project_id,
                    scope="project",
                    job_type="matrix.enrich",
                    status="running",
                    idempotency_scope_key=str(self.project_id),
                    idempotency_key="gateway-embedding-test",
                )
            )
            session.add(
                WorkflowJob(
                    id=self.image_job_id,
                    user_id=self.user_id,
                    project_id=self.project_id,
                    scope="project",
                    job_type="figures.redraw",
                    status="running",
                    idempotency_scope_key=str(self.project_id),
                    idempotency_key="gateway-image-test",
                )
            )
        self.settings = ApiSettings(
            review_root=Path(self.temporary.name),
            deployment_mode="hosted",
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            text_provider_api_key="server-secret",
            image_provider_api_key="server-image-secret",
            image_provider_base_url="https://images.example/v1",
            image_provider_model="image-test-model",
            image_provider_wire_api="chat-completions",
            image_provider_price_usd_per_image=Decimal("0.125"),
            embedding_provider_api_key="server-embedding-secret",
            embedding_provider_base_url="https://embeddings.example/v1",
            embedding_provider_model="embedding-test-model",
            embedding_provider_dimension=3,
            embedding_provider_price_usd_per_million=Decimal("0.10"),
            internal_gateway_url="http://127.0.0.1:8770/api/internal/v1/model-responses",
        )
        self.service = ModelGatewayService(self.sessions, self.settings)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    async def asyncTearDown(self) -> None:
        await self.service.close()

    def token(self) -> str:
        return self.service.issue_task_token(
            job_id=str(self.job_id),
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_type="draft.evaluate",
        )

    def image_token(self) -> str:
        return self.service.issue_task_token(
            job_id=str(self.image_job_id),
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_type="figures.redraw",
        )

    def embedding_token(self) -> str:
        return self.service.issue_task_token(
            job_id=str(self.embedding_job_id),
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_type="matrix.enrich",
        )

    def test_cost_uses_cached_input_price(self) -> None:
        cost = calculate_provider_cost(
            resolve_model_tier("terra"),
            input_tokens=1_000_000,
            cached_input_tokens=250_000,
            output_tokens=100_000,
        )
        self.assertEqual("2.75000000", format(cost, "f"))

    def test_tampered_task_token_is_rejected(self) -> None:
        token = self.token()
        with self.assertRaises(InvalidTaskToken):
            self.service.verify_task_token(token + "x")

    def test_project_model_tier_is_snapshotted_into_new_task_token(self) -> None:
        repository = HostedProjectRepository(self.sessions)
        updated = repository.update_model_tier_for_user(
            str(self.user_id), str(self.project_id), model_tier="luna"
        )
        claims = self.service.verify_task_token(self.token())

        self.assertEqual("luna", updated.model_tier)
        self.assertEqual("luna", claims.model_tier)

    def test_discovery_task_token_can_use_embedding_gateway(self) -> None:
        token = self.service.issue_task_token(
            job_id=str(self.job_id),
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_type="discovery.search",
        )

        claims = self.service.verify_task_token(token)

        self.assertIn("text", claims.capabilities)
        self.assertIn("embedding", claims.capabilities)

    def test_semantic_backfill_uses_ingest_queue_and_embedding_capability(self) -> None:
        token = self.service.issue_task_token(
            job_id=str(self.embedding_job_id),
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_type="library.semantic-backfill",
        )

        claims = self.service.verify_task_token(token)

        self.assertEqual("ingest", queue_for_job_type("library.semantic-backfill"))
        self.assertIn("embedding", claims.capabilities)
        self.assertNotIn("text", claims.capabilities)

    def test_worker_token_is_bound_to_current_lease_generation(self) -> None:
        lease_token = uuid.uuid4()
        with database_session(self.sessions) as session:
            job = session.get(WorkflowJob, self.job_id)
            job.lease_token = lease_token
            job.lease_generation = 4
            job.lease_expires_at = utc_now() + timedelta(minutes=5)
        token = self.service.issue_leased_task_token(
            job_id=str(self.job_id),
            lease_token=str(lease_token),
            lease_generation=4,
        )
        claims = self.service.verify_task_token(token)
        self.service._validate_live_job(claims)
        self.assertEqual(str(lease_token), claims.lease_token)
        self.assertEqual(4, claims.lease_generation)

        with database_session(self.sessions) as session:
            session.get(WorkflowJob, self.job_id).lease_generation = 5
        with self.assertRaises(InvalidTaskToken):
            self.service._validate_live_job(claims)

    async def test_success_is_metered_and_same_request_is_replayed(self) -> None:
        provider_result = {
            "id": "resp_test",
            "output_text": '{"score": 92}',
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        }
        with mock.patch.object(
            self.service,
            "_provider_call",
            new=mock.AsyncMock(return_value=provider_result),
        ) as provider_call:
            first = await self.service.complete_json(
                self.token(),
                request_key="score-batch-1",
                stage="evaluation",
                prompt="Return a score.",
            )
            second = await self.service.complete_json(
                self.token(),
                request_key="score-batch-1",
                stage="evaluation",
                prompt="Return a score.",
            )

        self.assertEqual(1, provider_call.await_count)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(150, first["usage"]["total_tokens"])
        summary = self.service.usage_summary(str(self.user_id), str(self.project_id))
        self.assertEqual(1, summary["request_count"])
        self.assertEqual(150, summary["total_tokens"])
        self.assertEqual("record_only", summary["billing_mode"])
        timeline = self.service.usage_timeline(
            str(self.user_id), str(self.project_id), days=30
        )
        self.assertEqual(30, len(timeline["items"]))
        self.assertEqual(150, timeline["items"][-1]["total_tokens"])
        self.assertEqual(1, timeline["items"][-1]["request_count"])

    async def test_embedding_request_is_task_bound_metered_and_cached(self) -> None:
        provider_result = {
            "id": "emb_test",
            "model": "embedding-test-model",
            "data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            ],
            "usage": {"prompt_tokens": 8, "total_tokens": 8},
        }
        with mock.patch.object(
            self.service,
            "_provider_embedding_call",
            new=mock.AsyncMock(return_value=provider_result),
        ) as provider_call:
            first = await self.service.complete_embeddings(
                self.embedding_token(),
                request_key="matrix-query-vectors",
                stage="matrix.enrich.embedding",
                inputs=["copper catalysis", "enantioselective allenation"],
            )
            second = await self.service.complete_embeddings(
                self.embedding_token(),
                request_key="matrix-query-vectors",
                stage="matrix.enrich.embedding",
                inputs=["copper catalysis", "enantioselective allenation"],
            )

        self.assertEqual(1, provider_call.await_count)
        self.assertEqual(3, first["dimension"])
        self.assertEqual(2, len(first["embeddings"]))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        summary = self.service.usage_summary(str(self.user_id), str(self.project_id))
        self.assertEqual(1, summary["request_count"])
        self.assertEqual(8, summary["total_tokens"])

    async def test_embedding_provider_requests_configured_dimension(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request):
            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )

        await self.service._provider_client.aclose()
        self.service._provider_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        result = await self.service._provider_embedding_call(
            inputs=["retrieval query"],
            idempotency_key="embedding-dimension-test",
        )

        self.assertEqual(3, captured["dimensions"])
        self.assertEqual("embedding-test-model", captured["model"])
        self.assertEqual(1, len(result["data"]))

    async def test_credit_is_reserved_settled_and_not_charged_again_on_cache_hit(self) -> None:
        billing = BillingService(self.sessions)
        billing.adjust(
            actor_user_id=self.user_id,
            target_user_id=self.user_id,
            amount_usd="1",
            reason="Gateway test credit",
            idempotency_key="gateway-credit",
        )
        self.service.billing_service = billing
        provider_result = {
            "id": "resp_billed",
            "output_text": '{"score": 92}',
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "input_tokens_details": {"cached_tokens": 20},
            },
        }
        with mock.patch.object(
            self.service,
            "_provider_call",
            new=mock.AsyncMock(return_value=provider_result),
        ) as provider_call:
            first = await self.service.complete_json(
                self.token(),
                request_key="billed-request",
                stage="evaluation",
                prompt="Return a score.",
            )
            second = await self.service.complete_json(
                self.token(),
                request_key="billed-request",
                stage="evaluation",
                prompt="Return a score.",
            )

        self.assertEqual(1, provider_call.await_count)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual("0.99943600", billing.account_summary(self.user_id)["balance_usd"])
        self.assertEqual("0.00000000", billing.account_summary(self.user_id)["reserved_usd"])
        self.assertEqual(3, len(billing.transactions(self.user_id)))
        self.assertEqual(
            "credit",
            self.service.usage_summary(str(self.user_id), str(self.project_id))["billing_mode"],
        )

    async def test_concurrent_same_request_joins_and_reuses_first_result(self) -> None:
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()
        provider_result = {
            "id": "resp_joined",
            "output_text": '{"joined": true}',
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }

        async def provider_call(**_kwargs):
            provider_started.set()
            await release_provider.wait()
            return provider_result

        with mock.patch.object(
            self.service, "_provider_call", side_effect=provider_call
        ) as provider:
            first = asyncio.create_task(
                self.service.complete_json(
                    self.token(),
                    request_key="joined-request",
                    stage="section-academic-planning",
                    prompt="Return a section plan.",
                )
            )
            await asyncio.wait_for(provider_started.wait(), timeout=1)
            second = asyncio.create_task(
                self.service.complete_json(
                    self.token(),
                    request_key="joined-request",
                    stage="section-academic-planning",
                    prompt="Return a section plan.",
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(second.done())
            release_provider.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(1, provider.await_count)
        self.assertFalse(first_result["cached"])
        self.assertTrue(second_result["cached"])
        self.assertEqual(first_result["output_text"], second_result["output_text"])

    async def test_provider_client_injects_server_key_and_selected_model(self) -> None:
        observed = {}

        def provider(request: httpx.Request) -> httpx.Response:
            observed["authorization"] = request.headers.get("Authorization")
            observed["payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": "resp_provider",
                    "output_text": '{"ok": true}',
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        await self.service._provider_client.aclose()
        self.service._provider_client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        )
        result = await self.service._provider_call(
            tier=resolve_model_tier("sol"),
            prompt="test",
            idempotency_key="job:request",
        )

        self.assertEqual("resp_provider", result["id"])
        self.assertEqual("Bearer server-secret", observed["authorization"])
        self.assertEqual("gpt-5.6-sol", observed["payload"]["model"])

    async def test_text_response_format_does_not_add_json_instruction(self) -> None:
        observed = {}

        def provider(request: httpx.Request) -> httpx.Response:
            observed["payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": "resp_text",
                    "output_text": "plain draft",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            )

        await self.service._provider_client.aclose()
        self.service._provider_client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        )
        result = await self.service._provider_call(
            tier=resolve_model_tier("terra"),
            prompt="write a paragraph",
            idempotency_key="job:text",
            response_format="text",
        )

        self.assertEqual("plain draft", result["output_text"])
        self.assertEqual(
            "write a paragraph",
            observed["payload"]["input"][0]["content"],
        )

    async def test_image_request_is_cached_and_metered_separately(self) -> None:
        calls = 0

        def provider(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            payload = json.loads(request.content.decode("utf-8"))
            self.assertEqual("image-test-model", payload["model"])
            self.assertTrue(payload["stream"])
            return httpx.Response(
                200,
                json={
                    "id": "image_provider_1",
                    "choices": [
                        {
                            "message": {
                                "images": [
                                    {
                                        "image_url": {
                                            "url": "data:image/png;base64,aW1hZ2UtYnl0ZXM="
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                },
            )

        await self.service._provider_client.aclose()
        self.service._provider_client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        )
        arguments = {
            "request_key": "figure-1",
            "stage": "figures.redraw",
            "operation": "edit",
            "prompt": "clean the figure",
            "images": [{"mime_type": "image/png", "data_base64": "c291cmNl"}],
        }
        first = await self.service.complete_image(self.image_token(), **arguments)
        second = await self.service.complete_image(self.image_token(), **arguments)

        self.assertEqual(1, calls)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual("aW1hZ2UtYnl0ZXM=", first["image_base64"])
        self.assertEqual(first["image_base64"], second["image_base64"])
        summary = self.service.usage_summary(str(self.user_id), str(self.project_id))
        self.assertEqual(1, summary["image_request_count"])
        self.assertEqual(1, summary["image_count"])
        self.assertEqual("0.12500000", summary["estimated_image_cost_usd"])

    async def test_image_moderation_rejection_is_not_reported_as_transient_502(self) -> None:
        calls = 0

        def provider(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "内容被安全审核拦截（疑似成人内容）",
                        "type": "moderation_blocked",
                    }
                },
            )

        await self.service._provider_client.aclose()
        self.service._provider_client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        )

        with self.assertRaises(GatewaySafetyBlocked):
            await self.service.complete_image(
                self.image_token(),
                request_key="moderated-figure",
                stage="figures.redraw",
                operation="edit",
                prompt="edit an academic chemistry diagram",
                images=[{"mime_type": "image/png", "data_base64": "c291cmNl"}],
            )

        self.assertEqual(1, calls)

    async def test_text_task_token_cannot_use_image_gateway(self) -> None:
        with self.assertRaisesRegex(InvalidTaskToken, "not authorized for image"):
            await self.service.complete_image(
                self.token(),
                request_key="unauthorized-image",
                stage="draft.evaluate",
                operation="edit",
                prompt="edit",
                images=[{"mime_type": "image/png", "data_base64": "c291cmNl"}],
            )

    async def test_image_request_does_not_wait_for_text_gateway_slot(self) -> None:
        text_started = asyncio.Event()
        release_text = asyncio.Event()
        image_started = asyncio.Event()

        async def text_provider(**_kwargs):
            text_started.set()
            await release_text.wait()
            return {
                "id": "text-1",
                "output_text": "done",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

        async def image_provider(**_kwargs):
            image_started.set()
            return b"image", "image/png", "image-1", 1

        with (
            mock.patch.object(self.service, "_provider_call", side_effect=text_provider),
            mock.patch.object(
                self.service, "_provider_image_call", side_effect=image_provider
            ),
        ):
            text_task = asyncio.create_task(
                self.service.complete(
                    self.token(),
                    request_key="blocked-text",
                    stage="draft.evaluate",
                    prompt="wait",
                    response_format="text",
                )
            )
            await asyncio.wait_for(text_started.wait(), timeout=1)
            image_task = asyncio.create_task(
                self.service.complete_image(
                    self.image_token(),
                    request_key="parallel-image",
                    stage="figures.redraw",
                    operation="edit",
                    prompt="edit",
                    images=[
                        {"mime_type": "image/png", "data_base64": "c291cmNl"}
                    ],
                )
            )
            await asyncio.wait_for(image_started.wait(), timeout=1)
            release_text.set()
            await asyncio.gather(text_task, image_task)


if __name__ == "__main__":
    unittest.main()
