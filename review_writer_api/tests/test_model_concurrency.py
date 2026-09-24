import asyncio
import base64
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, User, database_session
from review_writer_api.model_concurrency import AdjustableLimiter, defaults, load
from review_writer_api.security import Principal, Role


class ModelConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.settings = ApiSettings(
            review_root=Path(self.temp.name), deployment_mode="hosted",
            database_url="sqlite+pysqlite:///:memory:", public_origin="http://testserver",
            credential_encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode(),
            hosted_workspace_root=Path(self.temp.name) / "users",
        )
        with database_session(self.sessions) as session:
            admin = User(email=f"admin-{uuid.uuid4().hex}@example.com", password_hash="test", role="admin")
            user = User(email=f"user-{uuid.uuid4().hex}@example.com", password_hash="test", role="user")
            session.add_all([admin, user])
            session.flush()
            self.admin = Principal(str(admin.id), frozenset({Role.ADMIN}))
            self.user = Principal(str(user.id), frozenset({Role.USER}))

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_admin_update_validates_limits_and_persists_across_app_restart(self):
        selected = {**defaults(self.settings), "text": {"global": 4, "user": 2}}
        app = create_app(self.settings, principal_provider=lambda: self.admin,
            session_factory_override=self.sessions)
        with TestClient(app) as client:
            initial = client.get("/api/v1/admin/model-concurrency")
            self.assertEqual(200, initial.status_code)
            self.assertEqual(0, initial.json()["version"])
            invalid = client.put("/api/v1/admin/model-concurrency",
                json={"limits": {**selected, "text": {"global": 1, "user": 2}}})
            self.assertEqual(422, invalid.status_code)
            saved = client.put("/api/v1/admin/model-concurrency", json={"limits": selected})
            self.assertEqual(200, saved.status_code)
            self.assertEqual(1, saved.json()["version"])
            queues = client.get("/api/v1/admin/workers")
            self.assertEqual(200, queues.status_code)
            self.assertFalse(queues.json()["paused_queues"]["scientific"])
            paused = client.put("/api/v1/admin/workers/queues/scientific", json={"paused": True})
            self.assertEqual(200, paused.status_code)
            self.assertTrue(paused.json()["paused_queues"]["scientific"])
        self.assertEqual(selected, load(self.sessions, self.settings)["limits"])
        restricted = create_app(self.settings, principal_provider=lambda: self.user,
            session_factory_override=self.sessions)
        with TestClient(restricted) as client:
            self.assertEqual(403, client.get("/api/v1/admin/model-concurrency").status_code)
            self.assertEqual(403, client.put("/api/v1/admin/model-concurrency",
                json={"limits": selected}).status_code)
            self.assertEqual(403, client.put("/api/v1/admin/workers/queues/scientific",
                json={"paused": False}).status_code)


class AdjustableLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_requests_run_and_waiting_request_fills_released_slot(self):
        limiter = AdjustableLimiter(4)
        release = [asyncio.Event() for _ in range(5)]
        started = [asyncio.Event() for _ in range(5)]
        async def request(i):
            async with limiter:
                started[i].set()
                await release[i].wait()
        tasks = [asyncio.create_task(request(i)) for i in range(5)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started[:4])), 1)
            self.assertEqual(4, limiter.active)
            self.assertFalse(started[4].is_set())
            release[0].set()
            await asyncio.wait_for(started[4].wait(), 1)
            self.assertEqual(4, limiter.active)
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*tasks)

    async def test_waiting_same_user_does_not_consume_another_users_global_slot(self):
        shared = AdjustableLimiter(2)
        first_user = AdjustableLimiter(1)
        second_user = AdjustableLimiter(1)
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        async def first_call():
            async with first_user, shared:
                await release_first.wait()

        async def waiting_same_user():
            async with first_user, shared:
                second_started.set()

        first = asyncio.create_task(first_call())
        await asyncio.sleep(0)
        waiting = asyncio.create_task(waiting_same_user())
        await asyncio.sleep(0)
        async with second_user, shared:
            self.assertFalse(second_started.is_set())
            self.assertEqual(2, shared.active)
        release_first.set()
        await asyncio.gather(first, waiting)

    async def test_lowering_limit_drains_in_flight_calls_before_next_admission(self):
        limiter = AdjustableLimiter(2)
        first = await limiter.__aenter__()
        second = await limiter.__aenter__()
        self.assertIs(first, limiter)
        self.assertIs(second, limiter)
        await limiter.set_limit(1)
        started = asyncio.Event()

        async def next_call():
            async with limiter:
                started.set()

        task = asyncio.create_task(next_call())
        await asyncio.sleep(0)
        self.assertFalse(started.is_set())
        await limiter.__aexit__(None, None, None)
        await asyncio.sleep(0)
        self.assertFalse(started.is_set())
        await limiter.__aexit__(None, None, None)
        await asyncio.wait_for(task, 1)
        self.assertTrue(started.is_set())
