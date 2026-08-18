from __future__ import annotations

import base64
import concurrent.futures
import importlib
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings, database_url_from_env
from review_writer_api.database import Base, Project, User, create_session_factory, database_session
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import (
    WorkflowArtifact,
    WorkflowJob,
    WorkflowSystemState,
)


TEST_CREDENTIAL_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


def repository_api():
    try:
        errors = importlib.import_module("review_writer_api.errors")
        repository = importlib.import_module("review_writer_api.workflow_repository")
    except ModuleNotFoundError as exc:
        raise AssertionError("The isolated PostgreSQL workflow repository is missing.") from exc
    return errors, repository


def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


class WorkflowRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.errors, self.repository_module = repository_api()
        self.sessions, self.engine = session_factory()
        self.repository = self.repository_module.WorkflowRepository(self.sessions)
        with self.sessions.begin() as session:
            self.user_a = User(email="a@example.com", display_name="A", password_hash="hash")
            self.user_b = User(email="b@example.com", display_name="B", password_hash="hash")
            session.add_all([self.user_a, self.user_b])
            session.flush()
            self.project_a = Project(user_id=self.user_a.id, slug="same", topic="A")
            self.project_b = Project(user_id=self.user_b.id, slug="same", topic="B")
            session.add_all([self.project_a, self.project_b])
            session.flush()
            self.ids = {
                "user_a": str(self.user_a.id),
                "user_b": str(self.user_b.id),
                "project_a": str(self.project_a.id),
                "project_b": str(self.project_b.id),
            }

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_stage_reads_and_writes_are_project_owner_scoped(self) -> None:
        created = self.repository.compare_and_set_stage(
            self.ids["user_a"],
            self.ids["project_a"],
            "discovery",
            0,
            status="running",
        )

        self.assertEqual(1, created.revision)
        self.assertEqual(
            "running",
            self.repository.get_stage_state(
                self.ids["user_a"], self.ids["project_a"], "discovery"
            ).status,
        )
        self.assertIsNone(
            self.repository.get_stage_state(
                self.ids["user_b"], self.ids["project_a"], "discovery"
            )
        )
        with self.assertRaises(self.errors.WorkflowNotFound):
            self.repository.compare_and_set_stage(
                self.ids["user_b"],
                self.ids["project_a"],
                "discovery",
                1,
                status="completed",
            )

    def test_outdated_stage_revision_raises_conflict_without_mutating_state(self) -> None:
        self.repository.compare_and_set_stage(
            self.ids["user_a"], self.ids["project_a"], "matrix", 0, status="running"
        )

        with self.assertRaises(self.errors.WorkflowConflict) as caught:
            self.repository.compare_and_set_stage(
                self.ids["user_a"],
                self.ids["project_a"],
                "matrix",
                0,
                status="completed",
            )

        self.assertEqual({"expected_revision": 0, "actual_revision": 1}, caught.exception.details)
        stored = self.repository.get_stage_state(
            self.ids["user_a"], self.ids["project_a"], "matrix"
        )
        self.assertEqual(("running", 1), (stored.status, stored.revision))

    def test_matching_revision_updates_stage_and_advances_revision(self) -> None:
        first = self.repository.compare_and_set_stage(
            self.ids["user_a"], self.ids["project_a"], "blueprint", 0, status="running"
        )
        updated = self.repository.compare_and_set_stage(
            self.ids["user_a"],
            self.ids["project_a"],
            "blueprint",
            first.revision,
            status="approved",
            output_fingerprint="e" * 64,
        )

        self.assertEqual(("approved", 2), (updated.status, updated.revision))
        self.assertEqual("e" * 64, updated.output_fingerprint)

    def test_same_idempotency_key_returns_one_job_and_rejects_foreign_project(self) -> None:
        first = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "section-draft",
            "same-request",
            {"section_id": "s1"},
        )
        second = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "section-draft",
            "same-request",
            {"section_id": "s1"},
        )

        self.assertEqual(first.id, second.id)
        with self.sessions() as session:
            self.assertEqual(1, session.scalar(select(func.count()).select_from(WorkflowJob)))
        with self.assertRaises(self.errors.WorkflowNotFound):
            self.repository.create_or_get_job(
                self.ids["user_b"],
                self.ids["project_a"],
                "project",
                "section-draft",
                "foreign-project",
                {},
            )

    def test_idempotency_is_scoped_by_project_and_job_type_and_rejects_payload_changes(self) -> None:
        with self.sessions.begin() as session:
            second_project = Project(
                user_id=self.user_a.id, slug="second", topic="Second project"
            )
            session.add(second_project)
            session.flush()
            second_project_id = str(second_project.id)

        first = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "sections.generate",
            "reused-browser-key",
            {"section": "s1"},
        )
        other_project = self.repository.create_or_get_job(
            self.ids["user_a"],
            second_project_id,
            "project",
            "sections.generate",
            "reused-browser-key",
            {"section": "s1"},
        )
        other_type = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "figures.redraw",
            "reused-browser-key",
            {"figure": "F001"},
        )

        self.assertEqual(3, len({first.id, other_project.id, other_type.id}))
        with self.assertRaises(self.errors.WorkflowConflict):
            self.repository.create_or_get_job(
                self.ids["user_a"],
                self.ids["project_a"],
                "project",
                "sections.generate",
                "reused-browser-key",
                {"section": "different"},
            )

    def test_only_one_claim_can_transition_a_queued_job(self) -> None:
        queued = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "figure-redraw",
            "claim-once",
            {"figure_id": "F001"},
        )

        first_claim = self.repository.claim_job(queued.id)
        second_claim = self.repository.claim_job(queued.id)

        self.assertEqual("running", first_claim.status)
        self.assertIsNone(second_claim)

    def test_operation_keys_allow_different_active_figures_but_block_duplicates(self) -> None:
        first = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "figures.redraw",
            "figure-one",
            {"figure_ids": ["P001-F01"]},
            operation_key="figure:P001-F01",
        )
        second = self.repository.create_or_get_job(
            self.ids["user_a"],
            self.ids["project_a"],
            "project",
            "figures.redraw",
            "figure-two",
            {"figure_ids": ["P001-F02"]},
            operation_key="figure:P001-F02",
        )

        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.idempotency_scope_key, second.idempotency_scope_key)
        with self.assertRaises(self.errors.WorkflowConflict):
            self.repository.create_or_get_job(
                self.ids["user_a"],
                self.ids["project_a"],
                "project",
                "figures.redraw",
                "duplicate-figure-one",
                {"figure_ids": ["P001-F01"]},
                operation_key="figure:P001-F01",
            )

    def test_running_jobs_are_marked_interrupted_on_startup(self) -> None:
        first = self.repository.create_or_get_job(
            self.ids["user_a"], None, "library", "pdf-parse", "lib-one", {}
        )
        second = self.repository.create_or_get_job(
            self.ids["user_b"], None, "library", "pdf-parse", "lib-two", {}
        )
        self.repository.claim_job(first.id)
        self.repository.claim_job(second.id)

        self.assertEqual(2, self.repository.mark_running_jobs_interrupted())
        self.assertEqual(
            "interrupted", self.repository.get_job(self.ids["user_a"], first.id).status
        )

    def test_runs_artifacts_approvals_and_migration_ledger_round_trip(self) -> None:
        run = self.repository.create_stage_run(
            self.ids["user_a"],
            self.ids["project_a"],
            "figures",
            status="running",
            input_snapshot={"selected": ["F001"]},
        )
        self.assertIsNone(
            self.repository.get_stage_run(
                self.ids["user_b"], self.ids["project_a"], run.id
            )
        )

        artifact = self.repository.create_artifact(
            self.ids["user_a"],
            self.ids["project_a"],
            logical_name="figures/F001.svg",
            artifact_type="svg",
            relative_path=".artifacts/figures/F001.svg",
            content_sha256="c" * 64,
            size_bytes=42,
            mtime_ns=7,
            producer_stage="figures",
            producer_run_id=run.id,
        )
        self.repository.set_current_artifact(
            self.ids["user_a"],
            self.ids["project_a"],
            "figures/F001.svg",
            artifact.id,
        )
        self.assertEqual(
            artifact.id,
            self.repository.get_current_artifact(
                self.ids["user_a"], self.ids["project_a"], "figures/F001.svg"
            ).id,
        )
        self.assertIsNone(
            self.repository.get_current_artifact(
                self.ids["user_b"], self.ids["project_a"], "figures/F001.svg"
            )
        )

        approval = self.repository.record_approval(
            self.ids["user_a"],
            self.ids["project_a"],
            "figures",
            subject_type="artifact",
            subject_id=artifact.id,
            decision="approved",
            details={"manual": True},
        )
        self.assertEqual(("approved", True), (approval.decision, approval.details["manual"]))

        migration = self.repository.upsert_migration(
            "sqlite",
            "workspace/workflow.sqlite3",
            source_sha256="d" * 64,
            status="running",
            report={"rows": 2},
        )
        updated = self.repository.upsert_migration(
            "sqlite",
            "workspace/workflow.sqlite3",
            source_sha256="d" * 64,
            status="succeeded",
            report={"rows": 2, "validated": True},
        )
        self.assertEqual(migration.id, updated.id)
        self.assertTrue(updated.report["validated"])

    def test_readiness_is_required_only_after_legacy_inventory_is_recorded(self) -> None:
        self.assertTrue(self.repository.workflow_is_ready())
        self.repository.set_system_state("legacy_source_inventory", {"source_count": 2})
        self.assertFalse(self.repository.workflow_is_ready())
        with self.assertRaises(self.errors.WorkflowMigrationRequired):
            self.repository.require_workflow_ready()

        self.repository.set_system_state("workflow_ready", {"status": "ready"})
        self.assertTrue(self.repository.workflow_is_ready())


class WorkflowReadinessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.errors, self.repository_module = repository_api()
        self.sessions, self.engine = session_factory()
        with self.sessions.begin() as session:
            user = User(email="api@example.com", display_name="API", password_hash="hash")
            session.add(user)
            session.flush()
            self.principal = Principal(
                user_id=str(user.id),
                roles=frozenset({Role.USER}),
                email=user.email,
                display_name=user.display_name,
            )
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        settings = ApiSettings(
            review_root=root,
            deployment_mode="hosted",
            database_url="sqlite+pysqlite:///:memory:",
            public_origin="http://testserver",
            credential_encryption_key=TEST_CREDENTIAL_KEY,
            hosted_workspace_root=root / "hosted",
        )
        self.repository = self.repository_module.WorkflowRepository(self.sessions)
        self.app = create_app(
            settings,
            principal_provider=lambda: self.principal,
            session_factory_override=self.sessions,
            workflow_repository_override=self.repository,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_workflow_routes_return_nested_503_but_identity_and_health_remain_available(self) -> None:
        self.repository.set_system_state("legacy_source_inventory", {"source_count": 1})

        with TestClient(self.app) as client:
            blocked = client.get("/api/v1/projects")
            self.assertEqual(503, blocked.status_code)
            self.assertEqual(
                {
                    "error": {
                        "code": "WORKFLOW_MIGRATION_REQUIRED",
                        "message": "Workflow migration must complete before workflow access.",
                        "retryable": False,
                        "details": {},
                    }
                },
                blocked.json(),
            )
            self.assertEqual(200, client.get("/api/v1/health").status_code)
            self.assertEqual(200, client.get("/api/v1/me").status_code)


@unittest.skipUnless(
    os.environ.get("REVIEW_WRITER_RUN_POSTGRES_TESTS") == "1",
    "Set REVIEW_WRITER_RUN_POSTGRES_TESTS=1 for PostgreSQL transaction tests.",
)
class PostgreSQLWorkflowRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.errors, repository_module = repository_api()
        self.sessions, self.engine = create_session_factory(database_url_from_env())
        self.repository = repository_module.WorkflowRepository(self.sessions)
        marker = uuid.uuid4().hex
        with database_session(self.sessions) as session:
            user = User(
                email=f"repository-{marker}@example.com",
                display_name="Repository test",
                password_hash="hash",
            )
            session.add(user)
            session.flush()
            project = Project(user_id=user.id, slug=f"repo-{marker[:12]}", topic="test")
            session.add(project)
            session.flush()
            self.user_id = str(user.id)
            self.project_id = str(project.id)

    def tearDown(self) -> None:
        with database_session(self.sessions) as session:
            user = session.get(User, uuid.UUID(self.user_id))
            if user is not None:
                session.delete(user)
        self.engine.dispose()

    def test_two_postgresql_transactions_claim_a_job_only_once(self) -> None:
        queued = self.repository.create_or_get_job(
            self.user_id,
            self.project_id,
            "project",
            "transaction-claim",
            uuid.uuid4().hex,
            {},
        )
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            return self.repository.claim_job(queued.id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _index: claim(), range(2)))

        self.assertEqual(1, sum(item is not None for item in claims))

    def test_concurrent_artifact_publications_keep_both_versions_and_one_current_pointer(self) -> None:
        barrier = threading.Barrier(2)

        def publish(index: int):
            artifact_id = str(uuid.uuid4())
            barrier.wait(timeout=10)
            return self.repository.publish_artifact(
                user_id=self.user_id,
                project_id=self.project_id,
                artifact_id=artifact_id,
                logical_name="figures/F001.svg",
                artifact_type="svg",
                relative_path=f".artifacts/F001/{artifact_id}/F001.svg",
                content_sha256=f"{index + 1:064x}",
                size_bytes=index + 1,
                mtime_ns=index + 1,
                producer_stage="figures",
                producer_run_id=None,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            published = list(executor.map(publish, range(2)))

        current = self.repository.get_current_artifact(
            self.user_id, self.project_id, "figures/F001.svg"
        )
        self.assertIsNotNone(current)
        self.assertIn(current.id, {artifact.id for artifact in published})
        with database_session(self.sessions) as session:
            count = session.scalar(
                select(func.count())
                .select_from(WorkflowArtifact)
                .where(
                    WorkflowArtifact.project_id == uuid.UUID(self.project_id),
                    WorkflowArtifact.logical_name == "figures/F001.svg",
                )
            )
        self.assertEqual(2, count)

    def test_concurrent_planning_promotions_keep_pointer_and_revision_together(self) -> None:
        candidates = []
        for index in range(2):
            run = self.repository.create_stage_run(
                self.user_id,
                self.project_id,
                "matrix",
                status="succeeded",
            )
            artifact_id = str(uuid.uuid4())
            artifact = self.repository.publish_artifact(
                user_id=self.user_id,
                project_id=self.project_id,
                artifact_id=artifact_id,
                logical_name="matrix/literature_matrix.json",
                artifact_type="json",
                relative_path=f".artifacts/matrix/{artifact_id}/matrix.json",
                content_sha256=f"{index + 501:064x}",
                size_bytes=index + 1,
                mtime_ns=index + 1,
                producer_stage="matrix",
                producer_run_id=run.id,
                make_current=False,
            )
            candidates.append((artifact, run))
        barrier = threading.Barrier(2)

        def promote(candidate):
            artifact, run = candidate
            barrier.wait(timeout=10)
            try:
                state = self.repository.promote_stage_artifacts_atomically(
                    self.user_id,
                    self.project_id,
                    "matrix",
                    artifact_ids={"matrix/literature_matrix.json": artifact.id},
                    run_id=run.id,
                    expected_revision=0,
                )
                return "promoted", artifact, run, state
            except self.errors.WorkflowConflict:
                return "conflict", artifact, run, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(promote, candidates))

        self.assertEqual(1, sum(item[0] == "promoted" for item in outcomes))
        winner = next(item for item in outcomes if item[0] == "promoted")
        current = self.repository.get_current_artifact(
            self.user_id,
            self.project_id,
            "matrix/literature_matrix.json",
        )
        state = self.repository.get_stage_state(
            self.user_id, self.project_id, "matrix"
        )
        self.assertEqual(winner[1].id, current.id)
        self.assertEqual(winner[2].id, state.current_run_id)
        self.assertEqual(1, state.revision)
        blueprint_run = self.repository.create_stage_run(
            self.user_id,
            self.project_id,
            "blueprint",
            status="succeeded",
        )
        blueprint_id = str(uuid.uuid4())
        blueprint = self.repository.publish_artifact(
            user_id=self.user_id,
            project_id=self.project_id,
            artifact_id=blueprint_id,
            logical_name="blueprint/section_blueprint.json",
            artifact_type="json",
            relative_path=f".artifacts/blueprint/{blueprint_id}/blueprint.json",
            content_sha256=f"{999:064x}",
            size_bytes=1,
            mtime_ns=1,
            producer_stage="blueprint",
            producer_run_id=blueprint_run.id,
            make_current=False,
        )
        self.repository.promote_stage_artifacts_atomically(
            self.user_id,
            self.project_id,
            "blueprint",
            artifact_ids={"blueprint/section_blueprint.json": blueprint.id},
            run_id=blueprint_run.id,
            expected_revision=0,
            approve_stages={"matrix": 1},
        )
        matrix_state = self.repository.get_stage_state(
            self.user_id, self.project_id, "matrix"
        )
        self.assertEqual("approved", matrix_state.status)
        self.assertEqual(2, matrix_state.revision)

    def test_draft_gate_and_upstream_change_never_publish_unapproved_snapshot(self) -> None:
        def staged(stage: str, logical_name: str, seed: int, *, current: bool):
            run = self.repository.create_stage_run(
                self.user_id, self.project_id, stage, status="succeeded"
            )
            artifact = self.repository.publish_artifact(
                user_id=self.user_id,
                project_id=self.project_id,
                artifact_id=str(uuid.uuid4()),
                logical_name=logical_name,
                artifact_type="json" if logical_name.endswith(".json") else "markdown",
                relative_path=f".artifacts/{stage}/{seed}",
                content_sha256=f"{seed:064x}",
                size_bytes=seed,
                mtime_ns=seed,
                producer_stage=stage,
                producer_run_id=run.id,
                make_current=current,
            )
            return artifact, run

        sections, sections_run = staged(
            "sections", "sections/section_drafts.json", 1101, current=True
        )
        sections_state = self.repository.compare_and_set_stage(
            self.user_id,
            self.project_id,
            "sections",
            0,
            status="approved",
            current_run_id=sections_run.id,
        )
        manifest, figures_run = staged(
            "figures", "figures/manifest.json", 1102, current=True
        )
        figures_state = self.repository.compare_and_set_stage(
            self.user_id,
            self.project_id,
            "figures",
            0,
            status="approved",
            current_run_id=figures_run.id,
        )
        draft, draft_run = staged(
            "draft", "draft/manuscript.md", 1103, current=False
        )
        changed_sections, changed_run = staged(
            "sections", "sections/section_drafts.json", 1104, current=False
        )
        barrier = threading.Barrier(2)

        def assemble():
            barrier.wait(timeout=10)
            try:
                self.repository.promote_stage_artifacts_atomically(
                    self.user_id,
                    self.project_id,
                    "draft",
                    artifact_ids={"draft/manuscript.md": draft.id},
                    run_id=draft_run.id,
                    expected_revision=0,
                    expected_current_artifacts={
                        "sections/section_drafts.json": sections.id,
                        "figures/manifest.json": manifest.id,
                    },
                    expected_stage_states={
                        "figures": {
                            "revision": figures_state.revision,
                            "status": "approved",
                        }
                    },
                )
                return "assembled"
            except self.errors.WorkflowConflict:
                return "conflict"

        def change_sections():
            barrier.wait(timeout=10)
            self.repository.promote_stage_artifacts_atomically(
                self.user_id,
                self.project_id,
                "sections",
                artifact_ids={"sections/section_drafts.json": changed_sections.id},
                run_id=changed_run.id,
                expected_revision=sections_state.revision,
                status="approved",
                expected_current_artifacts={
                    "sections/section_drafts.json": sections.id
                },
                invalidate_stages=("figures", "draft", "final"),
            )
            return "changed"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda fn: fn(), (assemble, change_sections)))
        self.assertIn("changed", outcomes)
        self.assertIsNone(
            self.repository.get_current_artifact(
                self.user_id, self.project_id, "draft/manuscript.md"
            )
        )
        self.assertEqual(
            "pending",
            self.repository.get_stage_state(
                self.user_id, self.project_id, "figures"
            ).status,
        )

    def test_concurrent_discovery_replacements_promote_one_complete_transition(self) -> None:
        def stage_artifact(stage: str, logical_name: str, seed: int, *, current: bool):
            run = self.repository.create_stage_run(
                self.user_id,
                self.project_id,
                stage,
                status="succeeded",
            )
            artifact = self.repository.publish_artifact(
                user_id=self.user_id,
                project_id=self.project_id,
                artifact_id=str(uuid.uuid4()),
                logical_name=logical_name,
                artifact_type="json",
                relative_path=f".artifacts/{stage}/{seed}.json",
                content_sha256=f"{seed:064x}",
                size_bytes=seed,
                mtime_ns=seed,
                producer_stage=stage,
                producer_run_id=run.id,
                make_current=current,
            )
            return artifact, run

        initial, initial_run = stage_artifact(
            "discovery", "discovery/review.json", 101, current=True
        )
        self.repository.compare_and_set_stage(
            self.user_id,
            self.project_id,
            "discovery",
            0,
            status="review",
            current_run_id=initial_run.id,
        )
        matrix, matrix_run = stage_artifact(
            "matrix", "matrix/literature_matrix.json", 102, current=True
        )
        self.repository.compare_and_set_stage(
            self.user_id,
            self.project_id,
            "matrix",
            0,
            status="approved",
            current_run_id=matrix_run.id,
        )
        staged = [
            (*stage_artifact("discovery", "discovery/review.json", seed, current=False), topic)
            for seed, topic in ((201, "First replacement"), (202, "Second replacement"))
        ]
        barrier = threading.Barrier(2)

        def replace(candidate):
            artifact, run, topic = candidate
            barrier.wait(timeout=10)
            try:
                state = self.repository.replace_discovery_atomically(
                    self.user_id,
                    self.project_id,
                    artifact_id=artifact.id,
                    run_id=run.id,
                    expected_revision=1,
                    topic=topic,
                )
                return "promoted", artifact.id, topic, state.revision
            except self.errors.WorkflowConflict:
                return "conflict", artifact.id, topic, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(replace, staged))

        promoted = [item for item in outcomes if item[0] == "promoted"]
        self.assertEqual(1, len(promoted), outcomes)
        self.assertEqual(2, promoted[0][3])
        current = self.repository.get_current_artifact(
            self.user_id, self.project_id, "discovery/review.json"
        )
        self.assertEqual(promoted[0][1], current.id)
        self.assertEqual(
            matrix.id,
            self.repository.get_current_artifact(
                self.user_id, self.project_id, "matrix/literature_matrix.json"
            ).id,
        )
        self.assertEqual(
            "approved",
            self.repository.get_stage_state(
                self.user_id, self.project_id, "matrix"
            ).status,
        )
        with database_session(self.sessions) as session:
            project = session.get(Project, uuid.UUID(self.project_id))
            self.assertEqual("test", project.topic)
            self.assertEqual(2, project.stage_states["discovery"]["revision"])
            self.assertEqual("approved", project.stage_states["matrix"]["status"])

    def test_concurrent_discovery_save_and_restart_never_split_pointer_and_state(self) -> None:
        def stage(seed: int):
            run = self.repository.create_stage_run(
                self.user_id,
                self.project_id,
                "discovery",
                status="succeeded",
            )
            artifact = self.repository.publish_artifact(
                user_id=self.user_id,
                project_id=self.project_id,
                artifact_id=str(uuid.uuid4()),
                logical_name="discovery/review.json",
                artifact_type="json",
                relative_path=f".artifacts/discovery/{seed}.json",
                content_sha256=f"{seed:064x}",
                size_bytes=seed,
                mtime_ns=seed,
                producer_stage="discovery",
                producer_run_id=run.id,
                make_current=False,
            )
            return artifact, run

        initial_artifact, initial_run = stage(301)
        self.repository.set_current_artifact(
            self.user_id,
            self.project_id,
            "discovery/review.json",
            initial_artifact.id,
        )
        self.repository.compare_and_set_stage(
            self.user_id,
            self.project_id,
            "discovery",
            0,
            status="review",
            current_run_id=initial_run.id,
        )
        saved, save_run = stage(302)
        restarted, restart_run = stage(303)
        barrier = threading.Barrier(2)

        def save():
            barrier.wait(timeout=10)
            try:
                state = self.repository.save_discovery_atomically(
                    self.user_id,
                    self.project_id,
                    artifact_id=saved.id,
                    run_id=save_run.id,
                    expected_revision=1,
                    status="review",
                )
                return "save", "promoted", state
            except self.errors.WorkflowConflict:
                return "save", "conflict", None

        def restart():
            barrier.wait(timeout=10)
            try:
                state = self.repository.replace_discovery_atomically(
                    self.user_id,
                    self.project_id,
                    artifact_id=restarted.id,
                    run_id=restart_run.id,
                    expected_revision=1,
                    topic="Replacement topic",
                )
                return "restart", "promoted", state
            except self.errors.WorkflowConflict:
                return "restart", "conflict", None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [executor.submit(save), executor.submit(restart)]
            outcomes = [future.result(timeout=15) for future in outcomes]

        promoted = next(item for item in outcomes if item[1] == "promoted")
        self.assertEqual(1, sum(item[1] == "promoted" for item in outcomes))
        current = self.repository.get_current_artifact(
            self.user_id, self.project_id, "discovery/review.json"
        )
        state = self.repository.get_stage_state(
            self.user_id, self.project_id, "discovery"
        )
        expected_artifact = saved if promoted[0] == "save" else restarted
        expected_run = save_run if promoted[0] == "save" else restart_run
        self.assertEqual(expected_artifact.id, current.id)
        self.assertEqual(expected_run.id, state.current_run_id)
        self.assertEqual(2, state.revision)
        with database_session(self.sessions) as session:
            project = session.get(Project, uuid.UUID(self.project_id))
            self.assertEqual("test", project.topic)

    def test_concurrent_project_jobs_allow_only_one_active_job_type(self) -> None:
        barrier = threading.Barrier(2)

        def create(index: int):
            barrier.wait(timeout=10)
            try:
                job = self.repository.create_or_get_job(
                    self.user_id,
                    self.project_id,
                    "project",
                    "sections.concurrent",
                    f"concurrent-{index}-{uuid.uuid4()}",
                    {"index": index},
                )
                return "created", job.id
            except self.errors.WorkflowConflict as exc:
                return "conflict", exc.details.get("current_job_id")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(create, range(2)))

        self.assertEqual(["conflict", "created"], sorted(item[0] for item in outcomes))
        created_id = next(item[1] for item in outcomes if item[0] == "created")
        conflict_id = next(item[1] for item in outcomes if item[0] == "conflict")
        self.assertEqual(created_id, conflict_id)

    def test_concurrent_idempotency_is_scoped_and_same_scope_duplicates_collapse(self) -> None:
        with database_session(self.sessions) as session:
            second = Project(
                user_id=uuid.UUID(self.user_id),
                slug=f"second-{uuid.uuid4().hex[:10]}",
                topic="Second",
            )
            session.add(second)
            session.flush()
            second_project_id = str(second.id)

        cross_project_barrier = threading.Barrier(2)

        def create_for_project(project_id: str):
            cross_project_barrier.wait(timeout=10)
            return self.repository.create_or_get_job(
                self.user_id,
                project_id,
                "project",
                "scoped.idempotency",
                "same-browser-key",
                {"value": 1},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            cross_project = list(
                executor.map(create_for_project, (self.project_id, second_project_id))
            )
        self.assertNotEqual(cross_project[0].id, cross_project[1].id)

        duplicate_barrier = threading.Barrier(2)

        def duplicate():
            duplicate_barrier.wait(timeout=10)
            return self.repository.create_or_get_job(
                self.user_id,
                self.project_id,
                "project",
                "scoped.duplicate",
                "duplicate-key",
                {"value": 2},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            duplicates = list(executor.map(lambda _index: duplicate(), range(2)))
        self.assertEqual(duplicates[0].id, duplicates[1].id)

    def test_cancel_races_finish_in_a_terminal_state(self) -> None:
        for outcome in ("success", "failure"):
            with self.subTest(outcome=outcome):
                queued = self.repository.create_or_get_job(
                    self.user_id,
                    self.project_id,
                    "project",
                    f"race.{outcome}",
                    uuid.uuid4().hex,
                    {},
                )
                self.assertIsNotNone(self.repository.claim_job(queued.id))
                barrier = threading.Barrier(2)

                def finish():
                    barrier.wait(timeout=10)
                    if outcome == "success":
                        terminal = self.repository.mark_job_succeeded(queued.id, {"ok": True})
                    else:
                        terminal = self.repository.mark_job_failed(
                            queued.id,
                            error_code="EXPECTED_FAILURE",
                            error_message="Expected failure.",
                        )
                    if terminal is None:
                        self.repository.mark_job_cancelled(queued.id)

                def cancel():
                    barrier.wait(timeout=10)
                    self.repository.request_job_cancellation(self.user_id, queued.id)

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(lambda fn: fn(), (finish, cancel)))

                final = self.repository.get_job(self.user_id, queued.id)
                self.assertIn(final.status, {"succeeded", "failed", "cancelled"})
                self.assertNotEqual("cancel_requested", final.status)


if __name__ == "__main__":
    unittest.main()
