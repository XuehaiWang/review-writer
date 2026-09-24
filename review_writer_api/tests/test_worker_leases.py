from __future__ import annotations

import base64
import threading
import time
import tempfile
import unittest
import uuid
from concurrent.futures import Future
from unittest import mock
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.config import ApiSettings
from review_writer_api.billing import InsufficientCredit
from review_writer_api.database import Base, Project, User, utc_now
from review_writer_api.gateway_app import create_gateway_app
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_lease_context import bind_job_lease
from review_writer_api.repositories import HostedProjectRepository
from review_writer_api.worker_service import WorkerService
from review_writer_api.worker_wait_state import report_model_waiting
from review_writer_api.workflow_models import WorkflowJob
from review_writer_api.workflow_repository import WorkflowRepository


class WorkerLeaseTests(unittest.TestCase):
    def _model_job(self, key, user=None, project=None):
        return self.repository.create_or_get_job(
            user or self.first_user, project or self.first_project,
            "project", "model.dispatch", key, {}, operation_key=key,
        )

    def _text_limit(self, value):
        from review_writer_api.model_concurrency import save
        save(self.sessions, {"text": {"global": 4, "user": value},
            "image": {"global": 1, "user": 1},
            "embedding": {"global": 1, "user": 1}}, "test")

    def test_model_leases_fill_limit_release_slot_and_respect_live_shrink(self):
        self._text_limit(4)
        jobs = [self._model_job(f"request-{i}") for i in range(6)]
        claimed = [self.repository.claim_next_job(owner=f"worker-{i}",
            job_types={"model.dispatch"}) for i in range(4)]
        self.assertEqual({j.id for j in jobs[:4]}, {j.id for j in claimed})
        self.assertIsNone(self.repository.claim_next_job(owner="fifth", job_types={"model.dispatch"}))
        other = self._model_job("other-user", self.second_user, self.second_project)
        self.assertEqual(other.id, self.repository.claim_next_job(owner="other", job_types={"model.dispatch"}).id)
        self._text_limit(2)
        for job in claimed[:2]:
            self.repository.mark_job_succeeded(job.id, {}, lease_token=job.lease_token,
                lease_generation=job.lease_generation)
        self.assertIsNone(self.repository.claim_next_job(owner="at-limit", job_types={"model.dispatch"}))
        job = claimed[2]
        self.repository.mark_job_succeeded(job.id, {}, lease_token=job.lease_token,
            lease_generation=job.lease_generation)
        self.assertEqual(jobs[4].id, self.repository.claim_next_job(owner="refill", job_types={"model.dispatch"}).id)

    def test_worker_runs_four_same_user_model_jobs_before_any_finishes(self):
        self._text_limit(4)
        for i in range(5):
            self._model_job(f"parallel-{i}")
        ready, release = threading.Event(), threading.Event()
        started, lock = [], threading.Lock()
        def handler(context, payload):
            with lock:
                started.append(context.job_id)
                if len(started) == 4:
                    ready.set()
            if not release.wait(8):
                raise RuntimeError("Test did not release model calls")
            return {}
        worker = WorkerService(self.repository, {"model.dispatch": handler},
            max_workers=4, queues={"model"}, poll_seconds=0.05, heartbeat_seconds=2)
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(5), "Four independent calls should start together")
            self.assertEqual(4, len(started))
        finally:
            worker.stop()
            release.set()
            thread.join(timeout=5)

    def test_image_pool_does_not_overcommit_waiting_subprocesses(self):
        worker = WorkerService(
            self.repository, {"figures.redraw": lambda *_: {}},
            max_workers=2, queues={"image"},
        )
        try:
            worker._futures = {"A": Future(), "B": Future()}
            worker._set_model_waiting("A", True)
            worker._set_model_waiting("B", True)
            self.assertFalse(worker._can_claim())
        finally:
            worker._executor.shutdown(wait=False, cancel_futures=True)

    def test_model_wait_releases_only_bounded_business_capacity(self):
        worker = WorkerService(self.repository, {"sections.generate": lambda *_: {}}, max_workers=2)
        try:
            worker._futures = {"A": Future(), "B": Future()}
            self.assertFalse(worker._can_claim())
            worker._set_model_waiting("A", True)
            self.assertTrue(worker._can_claim())
            worker._futures["C"] = Future()
            self.assertFalse(worker._can_claim())
            worker._set_model_waiting("B", True)
            self.assertTrue(worker._can_claim())
            worker._futures["D"] = Future()
            self.assertFalse(worker._can_claim())
            self.assertEqual((4, 2), worker._activity_counts())
        finally:
            worker._executor.shutdown(wait=False, cancel_futures=True)

    def test_other_user_starts_while_first_job_waits_for_model(self):
        self._create(self.first_user, self.first_project, "sections.generate", "waiting-on-model")
        self._create(self.second_user, self.second_project, "sections.generate", "other-user")
        model_waiting, second_started, release = threading.Event(), threading.Event(), threading.Event()

        def handler(context, _payload):
            if context.user_id == self.first_user:
                report_model_waiting(True)
                model_waiting.set()
                release.wait(5)
                report_model_waiting(False)
            else:
                second_started.set()
            return {}

        worker = WorkerService(self.repository, {"sections.generate": handler}, max_workers=1,
                               poll_seconds=0.05, heartbeat_seconds=2, worker_id="wait-slot-test")
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(model_waiting.wait(5))
            self.assertTrue(second_started.wait(5))
            self.assertFalse(release.is_set())
        finally:
            release.set()
            worker.stop()
            thread.join(timeout=5)

    def test_compatibility_executor_wakes_planning_after_matrix(self):
        from review_writer_api.job_service import JobService
        matrix = self._create(self.first_user, self.first_project, "matrix.enrich", "compat-facts")
        blueprint = self._create(self.first_user, self.first_project, "planning.blueprint", "compat-plan")
        started, release, planned = threading.Event(), threading.Event(), threading.Event()
        def extract(context, payload):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release extraction")
            return {}
        def plan(context, payload):
            self.assertEqual("succeeded", self.repository.get_job(self.first_user, matrix.id).status)
            planned.set()
            return {}
        service = JobService(self.repository, max_workers=2)
        service.register_handler("matrix.enrich", extract)
        service.register_handler("planning.blueprint", plan)
        try:
            service.start()
            self.assertTrue(started.wait(3))
            self.assertFalse(planned.is_set())
            self.assertEqual("queued", self.repository.get_job(self.first_user, blueprint.id).status)
            release.set()
            self.assertTrue(planned.wait(5))
        finally:
            release.set()
            service.shutdown()

    def test_planning_waits_for_earlier_matrix_without_claiming_a_worker(self):
        matrix = self._create(self.first_user, self.first_project, "matrix.enrich", "facts")
        blueprint = self._create(self.first_user, self.first_project, "planning.blueprint", "plan")
        self.assertTrue(self.repository.planning_job_blocked(blueprint.id))
        self.assertIsNone(self.repository.claim_job(blueprint.id))
        self.assertIsNone(self.repository.claim_next_job(owner="planner", job_types={"planning.blueprint"}))
        other = self._create(self.second_user, self.second_project, "planning.blueprint", "other")
        self.assertEqual(other.id, self.repository.claim_next_job(owner="other", job_types={"planning.blueprint"}).id)
        claimed = self.repository.claim_job(matrix.id)
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.repository.claim_job(blueprint.id))
        self.repository.mark_job_succeeded(matrix.id, {}, lease_token=claimed.lease_token,
            lease_generation=claimed.lease_generation)
        self.assertFalse(self.repository.planning_job_blocked(blueprint.id))
        self.assertEqual(blueprint.id, self.repository.claim_job(blueprint.id).id)

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

    def test_bibliography_cannot_block_same_users_upload_slot(self):
        audit = self._create(self.first_user, self.first_project, "library.bibliography-audit", "audit")
        upload = self._create(self.first_user, self.first_project, "library.upload", "upload")
        claimed_audit = self.repository.claim_next_job(owner="bibliography-worker", job_types={"library.bibliography-audit"})
        self.assertEqual(audit.id, claimed_audit.id)
        self.assertEqual("bibliography", claimed_audit.queue_name)
        claimed_upload = self.repository.claim_next_job(owner="upload-worker", job_types={"library.upload"})
        self.assertEqual(upload.id, claimed_upload.id)
        self.assertEqual("ingest", claimed_upload.queue_name)
        worker = WorkerService(self.repository, {"library.upload": lambda *_: {}, "library.bibliography-audit": lambda *_: {}}, queues={"ingest"})
        self.assertEqual({"library.upload"}, worker.supported_job_types)

    def test_full_mineru_capacity_leaves_flex_worker_for_other_work(self):
        repository = WorkflowRepository(self.sessions, mineru_max_concurrency=1)
        first_upload = self._create(self.first_user, self.first_project, "library.upload", "upload-a")
        second_upload = self._create(self.second_user, self.second_project, "library.upload", "upload-b")
        index = self._create(self.second_user, self.second_project, "library.index", "index-b")
        self.assertEqual(first_upload.id, repository.claim_next_job(owner="ingest").id)
        self.assertEqual(index.id, repository.claim_next_job(owner="flex").id)
        self.assertEqual("queued", repository.get_job(self.second_user, second_upload.id).status)

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

    def test_yielded_chapter_reenters_after_another_users_waiting_job(self) -> None:
        first = self._create(self.first_user, self.first_project, "sections.generate", "chapter-a")
        claimed = self.repository.claim_next_job(owner="worker-a")
        self.assertEqual(first.id, claimed.id)
        waiting = self._create(self.second_user, self.second_project, "sections.generate", "chapter-b")
        self.repository.release_job_lease(
            claimed.id, lease_token=claimed.lease_token,
            lease_generation=claimed.lease_generation,
        )
        next_job = self.repository.claim_next_job(owner="worker-b")
        self.assertEqual(waiting.id, next_job.id)

    def test_interactive_job_goes_next_but_aged_batch_cannot_starve(self) -> None:
        batch = self._create(self.first_user, self.first_project, "sections.generate", "long-batch")
        first = self.repository.claim_next_job(owner="worker-a")
        interactive = self._create(self.first_user, self.first_project, "draft.rewrite", "user-dialogue")
        self.repository.release_job_lease(first.id, lease_token=first.lease_token,
                                          lease_generation=first.lease_generation)
        admitted = self.repository.claim_next_job(owner="worker-b")
        self.assertEqual(interactive.id, admitted.id)
        self.repository.mark_job_succeeded(admitted.id, {}, lease_token=admitted.lease_token,
                                           lease_generation=admitted.lease_generation)
        newer = self._create(self.first_user, self.first_project, "draft.rewrite", "second-dialogue")
        with self.sessions.begin() as session:
            session.get(WorkflowJob, uuid.UUID(batch.id)).updated_at = utc_now() - timedelta(minutes=3)
        admitted = self.repository.claim_next_job(owner="worker-c")
        self.assertEqual(batch.id, admitted.id)
        self.assertEqual("queued", self.repository.get_job(self.first_user, newer.id).status)

    def test_delayed_retry_does_not_occupy_worker_or_run_before_due_time(self) -> None:
        queued = self._create(self.first_user, self.first_project, "sections.generate", "rate-limited")
        claimed = self.repository.claim_next_job(owner="worker-a")
        delayed = self.repository.release_job_lease(
            claimed.id, lease_token=claimed.lease_token,
            lease_generation=claimed.lease_generation,
            delay_seconds=60, queue_reason="provider_rate_limit",
        )
        self.assertEqual("queued", delayed.status)
        self.assertEqual("provider_rate_limit", delayed.queue_reason)
        self.assertIsNotNone(delayed.next_run_at)
        self.assertEqual(1, self.repository.worker_status()["queue_counts"]["scientific"]["retry_waiting"])
        self.assertIsNone(self.repository.claim_next_job(owner="worker-b"))
        with self.sessions.begin() as session:
            session.get(WorkflowJob, uuid.UUID(queued.id)).next_run_at = utc_now() - timedelta(seconds=1)
        resumed = self.repository.claim_next_job(owner="worker-b")
        self.assertEqual(queued.id, resumed.id)
        self.assertIsNone(resumed.next_run_at)
        self.assertEqual("", resumed.queue_reason)

    def test_paused_queue_does_not_claim_new_jobs(self) -> None:
        scientific = self._create(self.first_user, self.first_project, "sections.generate", "paused-writing")
        ingest = self._create(self.second_user, self.second_project, "library.upload", "running-ingest")
        self.repository.set_worker_queue_paused("scientific", True)
        claimed = self.repository.claim_next_job(owner="worker-a")
        self.assertEqual(ingest.id, claimed.id)
        self.assertEqual("queued", self.repository.get_job(self.first_user, scientific.id).status)
        self.repository.set_worker_queue_paused("scientific", False)
        self.assertEqual(scientific.id, self.repository.claim_next_job(owner="worker-b").id)

    def _replacement_project(self) -> str:
        with self.sessions.begin() as session:
            project = Project(
                user_id=uuid.UUID(self.first_user), slug="replacement", topic="New review"
            )
            session.add(project)
            session.flush()
            return str(project.id)

    def test_delete_cancels_project_jobs_fences_writes_and_releases_user_slot(self) -> None:
        old = self._create(self.first_user, self.first_project, "matrix.enrich", "old")
        queued = self._create(self.first_user, self.first_project, "figures.redraw", "queued")
        claimed = self.repository.claim_job(old.id)
        new = self._create(self.first_user, self._replacement_project(), "discovery.search", "new")
        other = self._create(self.second_user, self.second_project, "matrix.enrich", "other")
        library = self.repository.create_or_get_job(
            self.first_user, None, "library", "library.ingest", "library", {}
        )

        deleted = HostedProjectRepository(self.sessions).delete_for_user(
            self.first_user, self.first_project
        )

        self.assertTrue(deleted)
        for job in (old, queued):
            result = self.repository.get_job(self.first_user, job.id)
            self.assertEqual("cancelled", result.status)
            self.assertTrue(result.cancellation_requested)
            self.assertIsNotNone(result.finished_at)
            self.assertIsNone(result.lease_token)
            self.assertIsNone(result.lease_expires_at)
        for job in (new, other, library):
            self.assertEqual("queued", self.repository.get_job(job.user_id, job.id).status)
        self.assertIsNone(self.repository.update_job_progress(
            old.id, 1, 2, lease_token=claimed.lease_token,
            lease_generation=claimed.lease_generation,
        ))
        self.assertIsNone(self.repository.mark_job_succeeded(
            old.id, {"late": True}, lease_token=claimed.lease_token,
            lease_generation=claimed.lease_generation,
        ))
        admitted = self.repository.claim_next_job(owner="new-worker", job_types={"discovery.search"})
        self.assertEqual(new.id, admitted.id)

    def test_legacy_deleted_project_lease_cannot_renew_or_block_new_search(self) -> None:
        old = self._create(self.first_user, self.first_project, "matrix.enrich", "legacy")
        queued = self._create(self.first_user, self.first_project, "figures.redraw", "legacy-queued")
        claimed = self.repository.claim_job(old.id)
        new = self._create(self.first_user, self._replacement_project(), "discovery.search", "new")
        with self.sessions.begin() as session:
            session.get(Project, uuid.UUID(self.first_project)).deleted_at = utc_now()

        self.assertTrue(self.repository.job_cancellation_requested(old.id))
        self.assertIsNone(self.repository.renew_job_lease(
            old.id, lease_token=claimed.lease_token,
            lease_generation=claimed.lease_generation,
        ))
        with bind_job_lease(claimed.id, claimed.lease_token, claimed.lease_generation):
            with self.assertRaises(WorkflowConflict):
                self.repository.require_bound_job_lease()
        admitted = self.repository.claim_next_job(owner="new-worker", job_types={"discovery.search"})
        self.assertEqual(new.id, admitted.id)
        for job in (old, queued):
            self.assertEqual("cancelled", self.repository.get_job(self.first_user, job.id).status)

    def test_legacy_executor_does_not_claim_deleted_project(self) -> None:
        old = self._create(self.first_user, self.first_project, "matrix.enrich", "legacy")
        with self.sessions.begin() as session:
            session.get(Project, uuid.UUID(self.first_project)).deleted_at = utc_now()
        self.assertIsNone(self.repository.claim_job(old.id))
        self.assertEqual("cancelled", self.repository.get_job(self.first_user, old.id).status)

    def test_reusing_deleted_slug_still_stops_removed_job(self) -> None:
        old = self._create(self.first_user, self.first_project, "matrix.enrich", "old")
        self.repository.claim_job(old.id)
        projects = HostedProjectRepository(self.sessions)
        projects.delete_for_user(self.first_user, self.first_project)
        projects.create_for_user(
            self.first_user, slug="lease-a", topic="Replacement", taxonomy_profile="general_academic"
        )
        with self.sessions() as session:
            self.assertIsNone(session.get(WorkflowJob, uuid.UUID(old.id)))
        self.assertTrue(self.repository.job_cancellation_requested(old.id))

    def test_single_worker_stops_deleted_project_and_runs_new_search(self) -> None:
        old = self._create(self.first_user, self.first_project, "matrix.enrich", "old")
        new = self._create(self.first_user, self._replacement_project(), "discovery.search", "new")
        started = threading.Event()
        stopped = threading.Event()
        searched = threading.Event()
        release = threading.Event()

        def enrich(context, _payload):
            started.set()
            try:
                while not release.wait(0.01):
                    context.checkpoint()
            finally:
                stopped.set()

        def search(_context, _payload):
            searched.set()
            return {"found": True}

        worker = WorkerService(
            self.repository, {"matrix.enrich": enrich, "discovery.search": search},
            max_workers=1, poll_seconds=0.05, lease_seconds=30, heartbeat_seconds=2,
            worker_id="delete-test-worker",
        )
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertFalse(searched.is_set())
            HostedProjectRepository(self.sessions).delete_for_user(self.first_user, self.first_project)
            self.assertTrue(stopped.wait(5))
            self.assertTrue(searched.wait(5))
            self.assertEqual("cancelled", self.repository.get_job(self.first_user, old.id).status)
            self.assertFalse(self.repository.job_cancellation_requested(new.id))
        finally:
            release.set()
            worker.stop()
            thread.join(timeout=5)

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

    def test_worker_unhandled_log_does_not_include_exception_secrets(self):
        queued = self._create(self.first_user, self.first_project, "sections.generate", "safe-log")
        claimed = self.repository.claim_next_job(owner="safe-log", lease_seconds=30,
                                                 job_types={"sections.generate"})
        def fail(context, payload):
            raise RuntimeError("sk-private-secret password=hidden response-body")
        worker = WorkerService(self.repository, {"sections.generate": fail}, max_workers=1)
        try:
            with self.assertLogs("review_writer_api.worker_service", level="ERROR") as captured:
                worker._execute(claimed)
            self.assertIn("RuntimeError", str(captured.output))
            for secret in ("sk-private-secret", "hidden", "response-body"):
                self.assertNotIn(secret, str(captured.output))
            self.assertEqual("failed", self.repository.get_job(self.first_user, queued.id).status)
        finally:
            worker._executor.shutdown(wait=False, cancel_futures=True)

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
            text_provider_api_key="test-provider-key",
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

            headers = {"Authorization": "Bearer " + issued.json()["task_token"]}
            with mock.patch.object(app.state.model_gateway, "billing_service", None), mock.patch.object(app.state.model_gateway, "_provider_call", new=mock.AsyncMock(
                return_value={"id": "late-result", "output_text": '{"facts": []}', "usage": {}}
            )) as provider:
                generated = client.post("/api/internal/v1/model-responses", headers=headers,
                                        json={"request_key": "late-facts", "stage": "facts", "prompt": "extract"})
                self.assertEqual(200, generated.status_code, generated.text)
                recovered = client.get("/api/internal/v1/model-responses/late-facts", headers=headers)
                self.assertEqual(200, recovered.status_code, recovered.text)
                self.assertEqual("succeeded", recovered.json()["status"])
                self.assertEqual(generated.json()["request_id"], recovered.json()["result"]["request_id"])
                self.assertTrue(recovered.json()["result"]["cached"])
                self.assertEqual(1, provider.await_count)
            self.assertEqual(401, client.get("/api/internal/v1/model-responses/late-facts").status_code)
            self.assertEqual(404, client.get("/api/internal/v1/model-responses/missing", headers=headers).status_code)

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
            self.assertEqual(401, client.get("/api/internal/v1/model-responses/late-facts", headers=headers).status_code)

    def test_private_gateway_preserves_insufficient_credit_status(self) -> None:
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
        with (
            mock.patch.object(
                app.state.model_gateway,
                "complete",
                new=mock.AsyncMock(
                    side_effect=InsufficientCredit(
                        "余额不足，无法开始本次外部模型调用。",
                        details={"available_usd": "0.01"},
                    )
                ),
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/internal/v1/model-responses",
                json={
                    "request_key": "section-S05",
                    "stage": "section-academic-planning",
                    "prompt": "plan",
                    "response_format": "json",
                },
            )

        self.assertEqual(402, response.status_code)
        self.assertEqual("INSUFFICIENT_CREDIT", response.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
