from __future__ import annotations

import base64
import concurrent.futures
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.errors import WorkflowConflict
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_repository import WorkflowRepository


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def job_service_class():
    try:
        from review_writer_api.job_service import JobService
    except ModuleNotFoundError as exc:
        raise AssertionError("The persisted bounded job service is missing.") from exc
    return JobService


class JobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "jobs.sqlite3"
        self.engine = create_engine(
            f"sqlite+pysqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            user = User(email="jobs@example.com", display_name="Jobs", password_hash="hash")
            other = User(email="other-jobs@example.com", display_name="Other", password_hash="hash")
            session.add_all([user, other])
            session.flush()
            project = Project(user_id=user.id, slug="copper", topic="Copper")
            session.add(project)
            session.flush()
            self.principal = Principal(
                user_id=str(user.id), roles=frozenset({Role.USER}), email=user.email
            )
            self.other_principal = Principal(
                user_id=str(other.id), roles=frozenset({Role.USER}), email=other.email
            )
            self.project_id = str(project.id)
        self.repository = WorkflowRepository(self.sessions)
        self.service = job_service_class()(self.repository, max_workers=2)

    def tearDown(self) -> None:
        self.service.shutdown(wait=True)
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _wait_for(self, job_id: str, statuses: set[str] = TERMINAL, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.service.status(self.principal, job_id)
            if job.status in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"Job {job_id} did not reach {sorted(statuses)}")

    def test_submit_is_idempotent_and_blocks_a_conflicting_project_job(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0
        lock = threading.Lock()

        def blocking(context, _payload):
            nonlocal calls
            with lock:
                calls += 1
            started.set()
            while not release.wait(0.01):
                context.checkpoint()
            return {"done": True}

        self.service.register_handler("sections.generate", blocking)
        self.service.start()
        first = self.service.submit(
            self.principal,
            scope="project",
            project_id=self.project_id,
            job_type="sections.generate",
            idempotency_key="same-click",
            payload={"section": "s1"},
        )
        duplicate = self.service.submit(
            self.principal,
            scope="project",
            project_id=self.project_id,
            job_type="sections.generate",
            idempotency_key="same-click",
            payload={"section": "s1"},
        )
        self.assertTrue(started.wait(2))
        self.assertEqual(first.id, duplicate.id)
        with self.assertRaises(WorkflowConflict):
            self.service.submit(
                self.principal,
                scope="project",
                project_id=self.project_id,
                job_type="sections.generate",
                idempotency_key="different-click",
                payload={"section": "s2"},
            )
        release.set()
        self.assertEqual("succeeded", self._wait_for(first.id).status)
        self.assertEqual(1, calls)

    def test_executor_is_bounded_and_persists_progress_and_success(self) -> None:
        active = 0
        maximum = 0
        lock = threading.Lock()
        two_started = threading.Event()
        release = threading.Event()

        def bounded(context, payload):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    two_started.set()
            context.report_progress(1, 2)
            try:
                while not release.wait(0.01):
                    context.checkpoint()
                context.report_progress(2, 2)
                return {"index": payload["index"]}
            finally:
                with lock:
                    active -= 1

        for index in range(4):
            self.service.register_handler(f"bounded.{index}", bounded)
        self.service.start()
        jobs = [
            self.service.submit(
                self.principal,
                scope="library",
                project_id=None,
                job_type=f"bounded.{index}",
                idempotency_key=f"bounded-{index}",
                payload={"index": index},
            )
            for index in range(4)
        ]
        self.assertTrue(two_started.wait(2))
        self.assertEqual(2, maximum)
        release.set()
        completed = [self._wait_for(job.id) for job in jobs]
        self.assertTrue(all(job.status == "succeeded" for job in completed))
        self.assertTrue(all(job.progress_current == 2 for job in completed))
        self.assertTrue(all(job.progress_total == 2 for job in completed))

    def test_cancellation_and_failure_are_persisted_without_leaking_exception_text(self) -> None:
        started = threading.Event()

        def cancellable(context, _payload):
            started.set()
            while True:
                context.checkpoint()
                time.sleep(0.01)

        self.service.register_handler("cancellable", cancellable)
        self.service.register_handler(
            "failing",
            lambda _context, _payload: (_ for _ in ()).throw(
                RuntimeError("provider secret sk-do-not-store")
            ),
        )
        self.service.start()
        cancelled = self.service.submit(
            self.principal,
            scope="project",
            project_id=self.project_id,
            job_type="cancellable",
            idempotency_key="cancel-me",
            payload={},
        )
        self.assertTrue(started.wait(2))
        requested = self.service.request_cancel(self.principal, cancelled.id)
        self.assertIn(requested.status, {"cancel_requested", "cancelled"})
        self.assertEqual("cancelled", self._wait_for(cancelled.id).status)

        failed = self.service.submit(
            self.principal,
            scope="library",
            project_id=None,
            job_type="failing",
            idempotency_key="fail-me",
            payload={},
        )
        final = self._wait_for(failed.id)
        self.assertEqual("failed", final.status)
        self.assertEqual("JOB_EXECUTION_FAILED", final.error_code)
        self.assertNotIn("sk-do-not-store", final.error_message)

    def test_startup_interrupts_running_work_and_retry_creates_a_linked_attempt(self) -> None:
        original = self.repository.create_or_get_job(
            self.principal.user_id,
            self.project_id,
            "project",
            "retryable",
            "original",
            {"value": 7},
        )
        self.assertIsNotNone(self.repository.claim_job(original.id))
        self.service.register_handler(
            "retryable", lambda context, payload: {"value": payload["value"]}
        )
        self.service.start()
        interrupted = self.service.status(self.principal, original.id)
        self.assertEqual("interrupted", interrupted.status)

        retried = self.service.retry_interrupted(self.principal, original.id)
        self.assertNotEqual(original.id, retried.id)
        self.assertEqual(original.id, retried.retry_of_job_id)
        final = self._wait_for(retried.id)
        self.assertEqual("succeeded", final.status)
        self.assertEqual({"value": 7}, final.result)

    def test_versioned_job_endpoints_are_user_scoped_and_support_cancel(self) -> None:
        settings = ApiSettings(
            review_root=Path(self.temporary.name),
            deployment_mode="hosted",
            database_url=f"sqlite+pysqlite:///{(Path(self.temporary.name) / 'jobs.sqlite3').as_posix()}",
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=Path(self.temporary.name) / "users",
        )
        current = self.principal
        app = create_app(
            settings,
            principal_provider=lambda: current,
            session_factory_override=self.sessions,
            job_service_override=self.service,
        )
        with TestClient(app) as client:
            queued = self.repository.create_or_get_job(
                self.principal.user_id,
                self.project_id,
                "project",
                "manual.queue",
                uuid.uuid4().hex,
                {},
            )
            response = client.get(f"/api/v1/jobs/{queued.id}")
            self.assertEqual(200, response.status_code)
            self.assertEqual(queued.id, response.json()["id"])
            cancelled = client.post(
                f"/api/v1/jobs/{queued.id}/cancel",
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(200, cancelled.status_code)
            self.assertEqual("cancelled", cancelled.json()["status"])

            current = self.other_principal
            hidden = client.get(f"/api/v1/jobs/{queued.id}")
            self.assertEqual(404, hidden.status_code)
            self.assertEqual("WORKFLOW_NOT_FOUND", hidden.json()["error"]["code"])

    def test_app_shutdown_has_a_bounded_grace_period_for_non_cooperative_work(self) -> None:
        bounded_service = job_service_class()(
            self.repository, max_workers=1, shutdown_grace_seconds=0.1
        )
        started = threading.Event()
        release = threading.Event()

        def non_cooperative(_context, _payload):
            started.set()
            release.wait(5)
            return {"released": True}

        bounded_service.register_handler("shutdown.blocked", non_cooperative)
        submitted_job: dict[str, str] = {}
        settings = ApiSettings(
            review_root=Path(self.temporary.name),
            deployment_mode="hosted",
            database_url=f"sqlite+pysqlite:///{(Path(self.temporary.name) / 'jobs.sqlite3').as_posix()}",
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=Path(self.temporary.name) / "shutdown-users",
        )
        app = create_app(
            settings,
            principal_provider=lambda: self.principal,
            session_factory_override=self.sessions,
            job_service_override=bounded_service,
        )

        def run_lifespan():
            with TestClient(app):
                submitted = bounded_service.submit(
                    self.principal,
                    scope="project",
                    project_id=self.project_id,
                    job_type="shutdown.blocked",
                    idempotency_key="shutdown-blocked",
                    payload={},
                )
                submitted_job["id"] = submitted.id
                self.assertTrue(started.wait(2))

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(run_lifespan)
        self.assertTrue(started.wait(2))
        returned_within_grace = True
        try:
            future.result(timeout=0.5)
        except concurrent.futures.TimeoutError:
            returned_within_grace = False
        finally:
            release.set()
            future.result(timeout=3)
            executor.shutdown(wait=True)
            bounded_service.shutdown(wait=False)

        self.assertTrue(returned_within_grace)
        stored = self.repository.get_job(
            self.principal.user_id, submitted_job["id"]
        )
        self.assertEqual("interrupted", stored.status)

    def test_non_cooperative_worker_does_not_block_python_process_exit(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        script = textwrap.dedent(
            """
            import tempfile
            import time
            from pathlib import Path

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            from review_writer_api.database import Base, Project, User
            from review_writer_api.job_service import JobService
            from review_writer_api.security import Principal, Role
            from review_writer_api.workflow_repository import WorkflowRepository

            temporary = tempfile.TemporaryDirectory()
            database_path = Path(temporary.name) / "process-exit.sqlite3"
            engine = create_engine(
                f"sqlite+pysqlite:///{database_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            with sessions.begin() as session:
                user = User(email="exit@example.com", display_name="Exit", password_hash="hash")
                session.add(user)
                session.flush()
                project = Project(user_id=user.id, slug="exit", topic="Exit")
                session.add(project)
                session.flush()
                principal = Principal(
                    user_id=str(user.id),
                    roles=frozenset({Role.USER}),
                    email=user.email,
                )
                project_id = str(project.id)

            repository = WorkflowRepository(sessions)
            service = JobService(repository, max_workers=1, shutdown_grace_seconds=0.05)

            def never_returns(_context, _payload):
                while True:
                    time.sleep(60)

            service.register_handler("shutdown.never", never_returns)
            job = service.submit(
                principal,
                scope="project",
                project_id=project_id,
                job_type="shutdown.never",
                idempotency_key="never",
                payload={},
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if repository.get_job(principal.user_id, job.id).status == "running":
                    break
                time.sleep(0.01)
            service.shutdown(wait=True)
            print("shutdown-returned", flush=True)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            self.fail(
                "Python process remained blocked by the job executor after shutdown. "
                f"stdout={stdout!r} stderr={stderr!r}"
            )

        self.assertEqual(0, process.returncode, stderr)
        self.assertIn("shutdown-returned", stdout)


if __name__ == "__main__":
    unittest.main()
