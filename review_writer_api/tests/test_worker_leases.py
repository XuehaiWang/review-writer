from __future__ import annotations

import base64
import threading
import time
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User, utc_now
from review_writer_api.gateway_app import create_gateway_app
from review_writer_api.worker_service import WorkerService
from review_writer_api.workflow_models import WorkflowJob
from review_writer_api.workflow_repository import WorkflowRepository


class WorkerLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.temporary.name}/worker-leases.sqlite3",
            connect_args={"check_same_thread": False, "timeout": 10},
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            first = User(email="lease-a@example.com", password_hash="hash")
            second = User(email="lease-b@example.com", password_hash="hash")
            session.add_all([first, second])
            session.flush()
            first_project = Project(user_id=first.id, slug="lease-a", topic="A")
            second_project = Project(user_id=second.id, slug="lease-b", topic="B")
            session.add_all([first_project, second_project])
            session.flush()
            self.first_user = str(first.id)
            self.second_user = str(second.id)
            self.first_project = str(first_project.id)
            self.second_project = str(second_project.id)
        self.repository = WorkflowRepository(self.sessions)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _create(self, user: str, project: str, job_type: str, key: str):
        return self.repository.create_or_get_job(
            user, project, "project", job_type, key, {}
        )

    def test_claim_is_fair_per_user_and_queue(self) -> None:
        first = self._create(
            self.first_user, self.first_project, "sections.generate", "a-1"
        )
        blocked_same_user = self._create(
            self.first_user, self.first_project, "draft.evaluate", "a-2"
        )
        other_user = self._create(
            self.second_user, self.second_project, "sections.generate", "b-1"
        )

        claimed_first = self.repository.claim_next_job(owner="worker-a")
        claimed_second = self.repository.claim_next_job(owner="worker-a")

        self.assertEqual(first.id, claimed_first.id)
        self.assertEqual(other_user.id, claimed_second.id)
        self.assertEqual(
            "queued",
            self.repository.get_job(self.first_user, blocked_same_user.id).status,
        )

    def test_twenty_users_each_receive_one_scientific_slot(self) -> None:
        users = [(self.first_user, self.first_project), (self.second_user, self.second_project)]
        with self.sessions.begin() as session:
            for index in range(2, 20):
                user = User(email=f"lease-{index}@example.com", password_hash="hash")
                session.add(user)
                session.flush()
                project = Project(
                    user_id=user.id, slug=f"lease-{index}", topic=str(index)
                )
                session.add(project)
                session.flush()
                users.append((str(user.id), str(project.id)))

        for index, (user_id, project_id) in enumerate(users):
            self._create(user_id, project_id, "sections.generate", f"first-{index}")
            self._create(user_id, project_id, "draft.evaluate", f"second-{index}")

        claimed = [
            self.repository.claim_next_job(owner=f"worker-{index}")
            for index in range(20)
        ]
        self.assertEqual(20, len({item.user_id for item in claimed if item}))
        self.assertIsNone(self.repository.claim_next_job(owner="worker-overflow"))

        first = claimed[0]
        self.repository.mark_job_succeeded(
            first.id,
            {"ok": True},
            lease_token=first.lease_token,
            lease_generation=first.lease_generation,
        )
        admitted = self.repository.claim_next_job(owner="worker-replacement")
        self.assertEqual(first.user_id, admitted.user_id)

    def test_expired_lease_is_reclaimed_and_old_writes_are_fenced(self) -> None:
        queued = self._create(
            self.first_user, self.first_project, "sections.generate", "fence"
        )
        old = self.repository.claim_next_job(owner="worker-old", lease_seconds=30)
        with self.sessions.begin() as session:
            row = session.get(WorkflowJob, uuid.UUID(old.id))
            row.lease_expires_at = utc_now() - timedelta(seconds=1)
        current = self.repository.claim_next_job(owner="worker-new", lease_seconds=30)

        self.assertEqual(queued.id, current.id)
        self.assertEqual(old.lease_generation + 1, current.lease_generation)
        self.assertIsNone(
            self.repository.update_job_progress(
                current.id,
                1,
                2,
                lease_token=old.lease_token,
                lease_generation=old.lease_generation,
            )
        )
        completed = self.repository.mark_job_succeeded(
            current.id,
            {"ok": True},
            lease_token=current.lease_token,
            lease_generation=current.lease_generation,
        )
        self.assertEqual("succeeded", completed.status)

    def test_independent_worker_executes_persisted_job(self) -> None:
        queued = self._create(
            self.first_user, self.first_project, "sections.generate", "worker"
        )

        def handler(context, _payload):
            context.report_progress(1, 1)
            return {"worker": True}

        worker = WorkerService(
            self.repository,
            {"sections.generate": handler},
            max_workers=1,
            poll_seconds=0.05,
            lease_seconds=30,
            heartbeat_seconds=2,
            worker_id="test-worker",
        )
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        final = None
        while time.monotonic() < deadline:
            final = self.repository.get_job(self.first_user, queued.id)
            if final.status == "succeeded":
                break
            time.sleep(0.02)
        worker.stop()
        thread.join(timeout=5)

        self.assertEqual("succeeded", final.status)
        self.assertEqual({"worker": True}, final.result)

    def test_worker_queue_filter_does_not_claim_an_unsupported_queue(self) -> None:
        image = self._create(
            self.first_user, self.first_project, "figures.redraw", "image-only"
        )
        scientific = self._create(
            self.second_user, self.second_project, "sections.generate", "text"
        )

        worker = WorkerService(
            self.repository,
            {
                "figures.redraw": lambda _context, _payload: {"image": True},
                "sections.generate": lambda _context, _payload: {"text": True},
            },
            max_workers=1,
            poll_seconds=0.05,
            lease_seconds=30,
            heartbeat_seconds=2,
            worker_id="scientific-only",
            queues={"scientific"},
        )
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        final = None
        while time.monotonic() < deadline:
            final = self.repository.get_job(self.second_user, scientific.id)
            if final.status == "succeeded":
                break
            time.sleep(0.02)
        worker.stop()
        thread.join(timeout=5)

        self.assertEqual("succeeded", final.status)
        self.assertEqual(
            "queued", self.repository.get_job(self.first_user, image.id).status
        )

    def test_private_gateway_exchanges_only_the_current_worker_lease(self) -> None:
        queued = self._create(
            self.first_user, self.first_project, "sections.generate", "gateway"
        )
        claimed = self.repository.claim_next_job(
            owner="gateway-worker", lease_seconds=30
        )
        settings = ApiSettings(
            review_root=Path(self.temporary.name),
            database_url=str(self.engine.url),
            credential_encryption_key=base64.urlsafe_b64encode(b"x" * 32).decode(
                "ascii"
            ),
            hosted_workspace_root=Path(self.temporary.name) / "workspaces",
            internal_worker_token="private-worker-secret",
        )
        app = create_gateway_app(settings)
        payload = {
            "job_id": claimed.id,
            "lease_token": claimed.lease_token,
            "lease_generation": claimed.lease_generation,
        }
        with TestClient(app) as client:
            unauthorized = client.post("/api/internal/v1/task-token", json=payload)
            self.assertEqual(401, unauthorized.status_code)

            issued = client.post(
                "/api/internal/v1/task-token",
                json=payload,
                headers={
                    "X-Review-Writer-Worker-Token": "private-worker-secret"
                },
            )
            self.assertEqual(200, issued.status_code)
            claims = app.state.model_gateway.verify_task_token(
                issued.json()["task_token"]
            )
            self.assertEqual(queued.id, claims.job_id)
            self.assertEqual(claimed.lease_generation, claims.lease_generation)

            self.repository.mark_job_succeeded(
                claimed.id,
                {"ok": True},
                lease_token=claimed.lease_token,
                lease_generation=claimed.lease_generation,
            )
            stale = client.post(
                "/api/internal/v1/task-token",
                json=payload,
                headers={
                    "X-Review-Writer-Worker-Token": "private-worker-secret"
                },
            )
            self.assertEqual(401, stale.status_code)


if __name__ == "__main__":
    unittest.main()
