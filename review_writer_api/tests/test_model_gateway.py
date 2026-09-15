from __future__ import annotations

import base64
import asyncio
import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx2 as httpx

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.billing import BillingService
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User, database_session, utc_now
from review_writer_api.model_catalog import resolve_model_tier
from review_writer_api.model_gateway import (
    GatewayBudgetExceeded,
    GatewayRequestConflict,
    GatewayRequestNotFound,
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
    async def test_dialogue_stream_publishes_before_completion(self):
        from review_writer_api.database import AIModelRequest
        service = self.service
        sessions = self.sessions
        observed = []

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                event = {"choices": [{"delta": {"content": '{"reply":"Hello'}}]}
                yield ("data: " + json.dumps(event) + "\n\n").encode()
                with database_session(sessions) as session:
                    row = session.scalar(select(AIModelRequest))
                    observed.append((row.status, row.response_json.get("partial_reply")))
                event = {"choices": [{"delta": {"content": ' world","candidate_text":""}'}}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
                yield ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()

        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())))
        runtime = replace(service._text_runtime(), wire_api="chat-completions")
        tier = replace(resolve_model_tier(None, self.sessions), wire_api="chat-completions")
        try:
            with mock.patch.object(service, "_provider_client", client), mock.patch.object(service, "_text_runtime", return_value=runtime), mock.patch.object(service, "_claims_model", return_value=tier):
                result = await service.complete(self.token(), request_key="stream", stage="Paragraph analysis and revision", prompt="reply")
            self.assertEqual(observed, [("running", "Hello")])
            self.assertEqual(json.loads(result["output_text"])["reply"], "Hello world")
            with database_session(sessions) as session:
                row = session.scalar(select(AIModelRequest))
                self.assertEqual(row.response_json["partial_reply"], "Hello world")
                self.assertEqual(row.response_json["stream_diagnostics"]["chunks"], 2)
                self.assertIsNotNone(row.response_json["stream_diagnostics"]["first_chunk_ms"])
        finally:
            await client.aclose()

    async def test_stream_protocol_completion_and_interruption(self):
        from review_writer_api.model_gateway import GatewayProviderError
        completed = {"output_text": '{"reply":"Ready"}', "usage": {"input_tokens": 3, "output_tokens": 4}}
        cases = [
            ("responses", [{"type": "response.output_text.delta", "delta": '{"reply":"Ready"}'},
                           {"type": "response.completed", "response": completed}], False),
            ("chat-completions", [{"choices": [{"delta": {"content": '{"reply":"Partial'}}]}], True),
        ]
        for wire, events, fails in cases:
            with self.subTest(wire=wire):
                body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))) as client:
                    with mock.patch.object(self.service, "_provider_client", client):
                        call = self.service._stream_text_response("https://provider.example/v1", {}, {}, str(uuid.uuid4()), wire, None)
                        if fails:
                            with self.assertRaises(GatewayProviderError):
                                await call
                        else:
                            self.assertEqual(await call, completed)

    async def test_structured_provider_quota_error_survives_original_result_lookup(self):
        from review_writer_api.model_gateway import GatewayProviderError
        from review_writer_api.schemas import ModelGatewayResultResponse
        reply = httpx.Response(503, json={"error": {"code": "insufficient_quota", "message": "Translated provider message"}})
        with mock.patch.object(self.service._provider_client, "post", new=mock.AsyncMock(return_value=reply)) as post:
            with self.assertRaises(GatewayProviderError) as failure:
                await self.service.complete(self.token(), request_key="quota", stage="rewrite", prompt="one")
        self.assertEqual(1, post.await_count)
        self.assertEqual("PROVIDER_QUOTA_EXHAUSTED", failure.exception.gateway_detail["code"])
        result = ModelGatewayResultResponse.model_validate(self.service.request_result(self.token(), request_key="quota"))
        self.assertEqual("failed", result.status)
        self.assertEqual("quota_exhausted", result.error["category"])
        self.assertEqual("insufficient_quota", result.error["provider_code"])
        self.assertEqual(503, result.error["provider_status"])
        self.assertNotIn("message", result.error)

    async def test_shared_budget_counts_internal_retries_and_keeps_cached_results(self):
        self.service.settings = replace(self.settings, text_job_max_provider_attempts=2)
        reply = {"output_text": "bounded result",
                 "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        post = mock.AsyncMock(side_effect=[httpx.Response(503), httpx.Response(200, json=reply)])
        with mock.patch.object(self.service._provider_client, "post", post), mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            first = await self.service.complete(self.token(), request_key="facts", stage="fact-verification", prompt="one")
            cached = await self.service.complete(self.token(), request_key="facts", stage="fact-verification", prompt="one")
            with self.assertRaises(GatewayBudgetExceeded):
                await self.service.complete(self.token(), request_key="rewrite", stage="paragraph-rewrite", prompt="two")
        self.assertEqual(2, first["provider_attempts"])
        self.assertEqual(6, first["provider_input_chars"])
        self.assertTrue(cached["cached"])
        self.assertEqual(2, post.await_count)

    async def test_input_budget_blocks_before_provider_request(self):
        self.service.settings = replace(self.settings, text_job_max_input_chars=3)
        with mock.patch.object(self.service._provider_client, "post", new=mock.AsyncMock()) as post:
            with self.assertRaises(GatewayBudgetExceeded):
                await self.service.complete(self.token(), request_key="too-large", stage="rewrite", prompt="four")
        post.assert_not_awaited()

    async def test_retry_job_id_does_not_reset_consumed_model_budget(self):
        self.service.settings = replace(self.settings, text_job_max_provider_attempts=1)
        post = mock.AsyncMock(return_value=httpx.Response(200, json={"output_text": "done"}))
        with mock.patch.object(self.service._provider_client, "post", post):
            await self.service.complete(self.token(), request_key="first", stage="facts", prompt="one")
            retry_id = uuid.uuid4()
            with database_session(self.sessions) as session:
                session.get(WorkflowJob, self.job_id).status = "failed"
                session.add(WorkflowJob(id=retry_id, user_id=self.user_id, project_id=self.project_id,
                    scope="project", job_type="draft.evaluate", status="running", retry_of_job_id=self.job_id,
                    idempotency_scope_key=str(self.project_id), idempotency_key="retry-budget"))
            token = self.service.issue_task_token(job_id=str(retry_id), user_id=str(self.user_id),
                                                 project_id=str(self.project_id), job_type="draft.evaluate")
            with self.assertRaises(GatewayBudgetExceeded):
                await self.service.complete(token, request_key="retry", stage="rewrite", prompt="two")
        self.assertEqual(1, post.await_count)

    async def test_cancel_during_provider_retry_prevents_next_paid_attempt(self):
        async def cancel_then_fail(*args, **kwargs):
            with database_session(self.sessions) as session:
                session.get(WorkflowJob, self.job_id).cancellation_requested = True
            return httpx.Response(503)
        post = mock.AsyncMock(side_effect=cancel_then_fail)
        with mock.patch.object(self.service._provider_client, "post", post), mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(GatewayRequestConflict):
                await self.service.complete(self.token(), request_key="cancel", stage="rewrite", prompt="one")
        self.assertEqual(1, post.await_count)

    async def test_lost_task_lease_prevents_another_provider_retry(self):
        post = mock.AsyncMock(return_value=httpx.Response(503))
        with mock.patch.object(self.service._provider_client, "post", post), \
             mock.patch.object(self.service, "_validate_live_job", side_effect=[None, None, InvalidTaskToken("lost lease")]), \
             mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(InvalidTaskToken):
                await self.service.complete(self.token(), request_key="lost-lease", stage="rewrite", prompt="one")
        self.assertEqual(1, post.await_count)

    async def test_deleted_project_stops_legacy_running_job_before_provider_call(self):
        token = self.token()
        with database_session(self.sessions) as session:
            session.get(Project, self.project_id).deleted_at = utc_now()
        with mock.patch.object(self.service._provider_client, "post", new=mock.AsyncMock()) as post:
            with self.assertRaises(InvalidTaskToken):
                await self.service.complete(token, request_key="deleted", stage="facts", prompt="one")
        post.assert_not_awaited()

    async def test_delete_during_provider_retry_prevents_next_attempt(self):
        async def delete_then_fail(*args, **kwargs):
            HostedProjectRepository(self.sessions).delete_for_user(str(self.user_id), str(self.project_id))
            return httpx.Response(503)

        post = mock.AsyncMock(side_effect=delete_then_fail)
        with mock.patch.object(self.service._provider_client, "post", post), mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(InvalidTaskToken):
                await self.service.complete(self.token(), request_key="delete", stage="facts", prompt="one")
        self.assertEqual(1, post.await_count)

    def test_deleted_project_cannot_exchange_a_legacy_live_lease_for_token(self):
        lease_token = uuid.uuid4()
        with database_session(self.sessions) as session:
            job = session.get(WorkflowJob, self.job_id)
            job.lease_token = lease_token
            job.lease_generation = 1
            job.lease_expires_at = utc_now() + timedelta(minutes=5)
            session.get(Project, self.project_id).deleted_at = utc_now()
        with self.assertRaises(InvalidTaskToken):
            self.service.issue_leased_task_token(
                job_id=str(self.job_id), lease_token=str(lease_token), lease_generation=1
            )

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

    def test_overview_token_allows_text_planning_and_image_generation(self) -> None:
        token = self.service.issue_task_token(
            job_id=str(self.job_id), user_id=str(self.user_id),
            project_id=str(self.project_id), job_type="final.overview",
        )
        claims = self.service.verify_task_token(token)
        self.assertIn("text", claims.capabilities)
        self.assertIn("image", claims.capabilities)
        self.assertNotIn("embedding", claims.capabilities)

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
            self.assertEqual({"status": "running", "result": None}, self.service.request_result(
                self.token(), request_key="joined-request"))
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
        recovered = self.service.request_result(self.token(), request_key="joined-request")
        self.assertEqual("succeeded", recovered["status"])
        self.assertEqual(first_result["request_id"], recovered["result"]["request_id"])
        self.assertTrue(recovered["result"]["cached"])

    async def test_result_lookup_is_task_bound_and_does_not_restart_failed_requests(self) -> None:
        from review_writer_api.database import AIModelRequest
        from review_writer_api.model_gateway import GatewayProviderError
        token = self.token()
        with mock.patch.object(self.service, "_provider_call", side_effect=GatewayProviderError("private upstream failure")) as provider:
            with self.assertRaises(GatewayProviderError):
                await self.service.complete(token, request_key="failed", stage="facts", prompt="one")
            for _ in range(2):
                snapshot = self.service.request_result(token, request_key="failed")
                self.assertEqual("failed", snapshot["status"])
                self.assertIsNone(snapshot["result"])
                self.assertEqual("transient", snapshot["error"]["category"])
                self.assertNotIn("private upstream failure", json.dumps(snapshot))
            with self.assertRaises(GatewayRequestNotFound):
                self.service.request_result(token, request_key="missing")
            with self.assertRaises(GatewayRequestNotFound):
                self.service.request_result(self.embedding_token(), request_key="failed")
        self.assertEqual(1, provider.await_count)
        with database_session(self.sessions) as session:
            row = session.scalar(select(AIModelRequest).where(AIModelRequest.job_id == self.job_id))
            self.assertEqual(1, row.attempt_count)
            session.get(WorkflowJob, self.job_id).cancellation_requested = True
        with self.assertRaises(GatewayRequestConflict):
            self.service.request_result(token, request_key="failed")
        with database_session(self.sessions) as session:
            session.get(WorkflowJob, self.job_id).status = "succeeded"
        with self.assertRaises(InvalidTaskToken):
            self.service.request_result(token, request_key="failed")

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

    async def test_image_quality_adapts_to_explicit_provider_requirement(self) -> None:
        for wire in ("images", "responses"):
            observed = []
            def provider(request):
                from urllib.parse import parse_qs
                if wire == "responses":
                    quality = json.loads(request.content)["tools"][0]["quality"]
                else:
                    quality = parse_qs(request.content.decode())["quality"][0]
                observed.append(quality)
                if quality != "low":
                    return httpx.Response(400, json={"error": {"message": "Invalid quality: This model supports only 'low'."}})
                return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"image" * 10).decode()}]})
            await self.service._provider_client.aclose()
            self.service._provider_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
            result = await self.service._provider_image_call(operation="generate", prompt="diagram", images=[], quality="high",
                background="auto", output_format="png", size="", idempotency_key="quality-test",
                runtime=replace(self.service._image_runtime(), wire_api=wire))
            self.assertEqual(["high", "low"], observed)
            self.assertEqual(b"image" * 10, result[0])
            self.assertEqual(2, result[3])

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
