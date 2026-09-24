import asyncio
import unittest
from collections import Counter
from dataclasses import replace
from unittest.mock import patch

from sqlalchemy import select

from review_writer_api.text_routing import TextChannelPool
from review_writer_api.database import AIModelRequest, database_session
from review_writer_api.model_catalog import resolve_model_tier
from review_writer_api.workflow_models import WorkflowSystemState
from review_writer_api.text_connections import CONNECTIONS_KEY
from review_writer_api.tests import test_model_gateway as gateway_fixtures


class ChannelPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_lowered_shared_connection_limit_applies_to_old_waiters(self):
        pool = TextChannelPool(4, 4)
        old = [{"connection_id": "A", "max_concurrency": 2, "capacity_revision": 1}]
        new = [{"connection_id": "A", "max_concurrency": 1, "capacity_revision": 2}]
        first, second = pool.admit("one", old), pool.admit("two", old)
        await first.__aenter__()
        await second.__aenter__()
        entered = asyncio.Event()
        async def wait():
            async with pool.admit("three", new):
                entered.set()
        pending = asyncio.create_task(wait())
        await asyncio.sleep(0)
        await first.__aexit__(None, None, None)
        await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        await second.__aexit__(None, None, None)
        await asyncio.wait_for(pending, 2)
        self.assertTrue(entered.is_set())
        async with pool.admit("one", old):
            self.assertEqual(pool.limits["A"], (2, 1))

    async def test_two_connections_share_four_slots_and_refill(self):
        pool = TextChannelPool(4, 4)
        channels = [{"connection_id": key, "max_concurrency": 2} for key in ("A", "B")]
        started = [asyncio.Event() for _ in range(5)]
        release = [asyncio.Event() for _ in range(5)]
        assignments = {}
        async def request(i):
            async with pool.admit("user", channels) as route:
                assignments[i] = route["connection_id"]
                started[i].set()
                await release[i].wait()
        tasks = [asyncio.create_task(request(i)) for i in range(5)]
        try:
            await asyncio.wait_for(asyncio.gather(*(s.wait() for s in started[:4])), 2)
            self.assertEqual(Counter(assignments.values()), {"A": 2, "B": 2})
            self.assertFalse(started[4].is_set())
            release[0].set()
            await asyncio.wait_for(started[4].wait(), 2)
            self.assertEqual(assignments[0], assignments[4])
            self.assertEqual(pool.active, 4)
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*tasks)
        self.assertEqual(pool.active, 0)

    async def test_busy_connection_waiter_does_not_reserve_global_capacity(self):
        pool = TextChannelPool(2, 2)
        a, b = {"connection_id": "A", "max_concurrency": 1}, {"connection_id": "B", "max_concurrency": 1}
        async with pool.admit("one", [a]):
            async def wait_a():
                async with pool.admit("two", [a]):
                    pass
            waiting = asyncio.create_task(wait_a())
            await asyncio.sleep(0)
            async with pool.admit("three", [b]):
                self.assertEqual(pool.active, 2)
                self.assertFalse(waiting.done())
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
        self.assertEqual(pool.active, 0)

    async def test_user_quota_and_shared_connection_quota_apply_across_models(self):
        pool = TextChannelPool(4, 1)
        a = [{"connection_id": "A", "max_concurrency": 2, "model": "model1"}]
        b = [{"connection_id": "A", "max_concurrency": 2, "model": "model2"}]
        async with pool.admit("one", a), pool.admit("two", b):
            self.assertEqual(pool.connections["A"], 2)
            async def pending():
                async with pool.admit("one", b):
                    pass
            waiting = asyncio.create_task(pending())
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
        await asyncio.wait_for(waiting, 2)


class ChannelGatewayTests(unittest.IsolatedAsyncioTestCase):
    setUp = gateway_fixtures.ModelGatewayTests.setUp
    tearDown = gateway_fixtures.ModelGatewayTests.tearDown
    asyncTearDown = gateway_fixtures.ModelGatewayTests.asyncTearDown
    token = gateway_fixtures.ModelGatewayTests.token

    def configure_channels(self):
        with database_session(self.sessions) as session:
            session.add(WorkflowSystemState(key=CONNECTIONS_KEY, value_json={"items": [
                {"id": key, "versions": [{"revision": 1, "max_concurrency": 2}]}
                for key in ("A", "B")]}))
        return replace(resolve_model_tier("terra"), channels=tuple({
            "connection_id": key, "connection_revision": 1,
            "model": f"vendor-{key}", "wire_api": "responses"} for key in ("A", "B")))

    async def test_gateway_records_actual_channels_and_cached_replay_does_not_resubmit(self):
        token = self.token()
        tier = self.configure_channels()
        release, ready = asyncio.Event(), asyncio.Event()
        used = []
        async def provider(*, tier, **kwargs):
            used.append((tier.connection_id, tier.model))
            if len(used) == 4:
                ready.set()
            await release.wait()
            return {"output_text": "{}", "usage": {"input_tokens": 1, "output_tokens": 1}}
        async def call(i):
            return await self.service.complete(token, request_key=str(i), stage="test", prompt="Evidence")
        with patch.object(self.service, "_claims_model", return_value=tier), patch.object(self.service, "_provider_call", side_effect=provider):
            tasks = [asyncio.create_task(call(i)) for i in range(4)]
            try:
                await asyncio.wait_for(ready.wait(), 2)
                self.assertEqual(Counter(key for key, _ in used), {"A": 2, "B": 2})
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
            replay = await call(0)
            self.assertTrue(replay["cached"])
            self.assertEqual(len(used), 4)
        self.assertEqual(len({r["cost_usd"] for r in results}), 1)
        with database_session(self.sessions) as session:
            rows = session.scalars(select(AIModelRequest)).all()
            self.assertEqual(len(rows), 4)
            for row in rows:
                self.assertEqual(row.model_name, f'vendor-{row.route_json["connection_id"]}')
                self.assertEqual(row.route_json["connection_revision"], 1)

    async def test_failed_request_remains_bound_to_original_channel_on_explicit_retry(self):
        token = self.token()
        tier = self.configure_channels()
        routes = []
        async def provider(*, tier, **kwargs):
            routes.append(tier.connection_id)
            if len(routes) == 1:
                raise RuntimeError("provider unavailable")
            return {"output_text": "{}", "usage": {}}
        with patch.object(self.service, "_claims_model", return_value=tier), patch.object(self.service, "_provider_call", side_effect=provider):
            with self.assertRaises(RuntimeError):
                await self.service.complete(token, request_key="retry", stage="test", prompt="Evidence")
            await self.service.complete(token, request_key="retry", stage="test", prompt="Evidence")
        self.assertEqual(routes, ["A", "A"])
