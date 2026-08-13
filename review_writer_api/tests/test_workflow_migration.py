from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.config import database_url_from_env
from review_writer_api.database import (
    Base,
    Project,
    User,
    create_session_factory,
    database_session,
)
from review_writer_api.workflow_models import (
    WorkflowArtifact,
    WorkflowArtifactDependency,
    WorkflowCurrentArtifact,
    WorkflowCurrentJob,
    WorkflowJob,
    WorkflowMigration,
    WorkflowStageRun,
    WorkflowStageState,
    WorkflowSystemState,
)
from view.workflow_store import WorkflowStore


NOW = "2026-08-01T10:00:00+00:00"


def migration_api():
    try:
        return importlib.import_module("review_writer_api.workflow_migration")
    except ModuleNotFoundError as exc:
        raise AssertionError("The stopped SQLite workflow migrator is missing.") from exc


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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_legacy_fixture(review_root: Path) -> dict[str, str]:
    store = WorkflowStore(review_root)
    database_path = store.database_path
    alpha_root = review_root / "review-projects" / "alpha"
    beta_root = review_root / "review-projects" / "beta"
    alpha_root.mkdir(parents=True, exist_ok=True)
    beta_root.mkdir(parents=True, exist_ok=True)
    shared_content = b'{"paper":"copper"}\n'
    alpha_file = alpha_root / "artifact.json"
    beta_file = beta_root / "duplicate.json"
    alpha_file.write_bytes(shared_content)
    beta_file.write_bytes(shared_content)

    ids = {
        "run_alpha": str(uuid.uuid4()),
        "run_beta": str(uuid.uuid4()),
        "artifact_alpha": str(uuid.uuid4()),
        "artifact_beta": str(uuid.uuid4()),
        "artifact_missing": str(uuid.uuid4()),
        "job_project": str(uuid.uuid4()),
        "job_library": str(uuid.uuid4()),
    }
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executemany(
            "INSERT INTO projects(project_id, root_path, created_at, updated_at) VALUES(?, ?, ?, ?)",
            [
                ("alpha", str(alpha_root), NOW, NOW),
                ("beta", str(beta_root), NOW, NOW),
                ("_library_", str(review_root / "review-library"), NOW, NOW),
            ],
        )
        connection.executemany(
            """
            INSERT INTO stage_runs(
                run_id, project_id, stage_id, status, attempt, input_fingerprint,
                input_snapshot_json, output_fingerprint, output_snapshot_json,
                progress_current, progress_total, error_message, metadata_json,
                started_at, updated_at, finished_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ids["run_alpha"], "alpha", "discovery", "completed", 1,
                    "a" * 64, json.dumps([{"logical_name": "project_config.json"}]),
                    "b" * 64, json.dumps([{"logical_name": "artifact.json"}]),
                    1, 1, "", json.dumps({"language": "zh"}), NOW, NOW, NOW,
                ),
                (
                    ids["run_beta"], "beta", "figures", "failed", 2,
                    "c" * 64, "[]", "", "[]", 1, 3, "rate limited",
                    json.dumps({"provider": "test"}), NOW, NOW, NOW,
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO stage_state(
                project_id, stage_id, status, current_run_id, input_fingerprint,
                output_fingerprint, error_message, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("alpha", "discovery", "completed", ids["run_alpha"], "a" * 64, "b" * 64, "", NOW),
                ("beta", "figures", "failed", ids["run_beta"], "c" * 64, "", "rate limited", NOW),
            ],
        )
        digest = sha256(shared_content)
        connection.executemany(
            """
            INSERT INTO artifact_versions(
                artifact_version_id, project_id, logical_name, artifact_type, path,
                content_sha256, size_bytes, mtime_ns, producer_stage, producer_run_id,
                metadata_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ids["artifact_alpha"], "alpha", "artifact.json", "json",
                    str(alpha_file), digest, len(shared_content), alpha_file.stat().st_mtime_ns,
                    "discovery", ids["run_alpha"], "{}", NOW,
                ),
                (
                    ids["artifact_beta"], "beta", "duplicate.json", "json",
                    str(beta_file), digest, len(shared_content), beta_file.stat().st_mtime_ns,
                    "sections", None, json.dumps({"duplicate_hash": True}), NOW,
                ),
                (
                    ids["artifact_missing"], "beta", "missing.png", "png",
                    str(beta_root / "missing.png"), "f" * 64, 99, 1,
                    "figures", ids["run_beta"], "{}", NOW,
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO current_artifacts(project_id, logical_name, artifact_version_id, updated_at) VALUES(?, ?, ?, ?)",
            [
                ("alpha", "artifact.json", ids["artifact_alpha"], NOW),
                ("beta", "missing.png", ids["artifact_missing"], NOW),
            ],
        )
        connection.execute(
            "INSERT INTO artifact_dependencies VALUES(?, ?, 'input')",
            (ids["artifact_missing"], ids["artifact_beta"]),
        )
        project_payload = {
            "job_id": ids["job_project"],
            "status": "completed",
            "progress_current": 2,
            "progress_total": 2,
            "result": {"sections": 2},
        }
        library_payload = {
            "job_id": ids["job_library"],
            "status": "failed",
            "error": "missing token",
        }
        connection.executemany(
            "INSERT INTO jobs(job_id, project_id, job_type, status, payload_json, started_at, updated_at, finished_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (ids["job_project"], "alpha", "section-draft", "completed", json.dumps(project_payload), NOW, NOW, NOW),
                (ids["job_library"], "_library_", "pdf-parse", "failed", json.dumps(library_payload), NOW, NOW, NOW),
            ],
        )
        connection.executemany(
            "INSERT INTO current_jobs(project_id, job_type, job_id, updated_at) VALUES(?, ?, ?, ?)",
            [
                ("alpha", "section-draft", ids["job_project"], NOW),
                ("_library_", "pdf-parse", ids["job_library"], NOW),
            ],
        )
        connection.commit()
    ids["database_path"] = str(database_path)
    ids["source_sha256"] = hashlib.sha256(database_path.read_bytes()).hexdigest()
    return ids


class WorkflowMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = migration_api()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace_root = self.root / "hosted-workspaces"
        self.workspace_root.mkdir()
        self.sessions, self.engine = session_factory()
        with self.sessions.begin() as session:
            user = User(email="owner@example.com", display_name="Owner", password_hash="hash")
            session.add(user)
            session.flush()
            self.user_id = str(user.id)
            session.add_all(
                [
                    Project(user_id=user.id, slug="alpha", topic="Alpha"),
                    Project(user_id=user.id, slug="beta", topic="Beta"),
                ]
            )
        self.review_root = self.workspace_root / self.user_id
        self.ids = create_legacy_fixture(self.review_root)
        self.backup_root = self.root / "backups"

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _count(self, model) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model))

    def test_inventory_and_dry_run_do_not_change_postgresql_rows(self) -> None:
        inventory = self.migration.inventory_legacy_workflows(
            self.workspace_root, self.sessions
        )
        self.assertEqual(1, inventory.source_count)
        self.assertEqual(2, inventory.table_counts["stage_runs"])
        self.assertEqual(3, inventory.table_counts["artifact_versions"])

        report = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            dry_run=True,
        )
        self.assertTrue(report.success)
        self.assertTrue(report.dry_run)
        self.assertFalse(report.ready)
        self.assertEqual(0, self._count(WorkflowStageRun))
        self.assertEqual(0, self._count(WorkflowSystemState))
        self.assertFalse(self.backup_root.exists())

    def test_import_preserves_rows_ids_json_and_requires_missing_file_acknowledgement(self) -> None:
        report = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=False,
        )

        self.assertTrue(report.success)
        self.assertFalse(report.ready)
        self.assertEqual(1, len(report.missing_files))
        self.assertEqual(self.ids["source_sha256"], report.sources[0].source_sha256)
        self.assertEqual(1, len(report.backup_paths))
        self.assertTrue(Path(report.backup_paths[0]).is_file())
        self.assertEqual(64, len(report.sources[0].backup_sha256))
        self.assertEqual(self.ids["source_sha256"], hashlib.sha256(Path(self.ids["database_path"]).read_bytes()).hexdigest())
        self.assertEqual(2, self._count(WorkflowStageRun))
        self.assertEqual(2, self._count(WorkflowStageState))
        self.assertEqual(3, self._count(WorkflowArtifact))
        self.assertEqual(1, self._count(WorkflowArtifactDependency))
        self.assertEqual(2, self._count(WorkflowCurrentArtifact))
        self.assertEqual(2, self._count(WorkflowJob))
        self.assertEqual(2, self._count(WorkflowCurrentJob))
        self.assertEqual(1, self._count(WorkflowMigration))

        with self.sessions() as session:
            run = session.get(WorkflowStageRun, uuid.UUID(self.ids["run_alpha"]))
            self.assertEqual("zh", run.metadata_json["language"])
            self.assertEqual(self.ids["run_alpha"], run.legacy_id)
            missing = session.get(WorkflowArtifact, uuid.UUID(self.ids["artifact_missing"]))
            self.assertEqual("missing", missing.availability)
            self.assertEqual("missing.png", missing.relative_path)
            library_job = session.get(WorkflowJob, uuid.UUID(self.ids["job_library"]))
            self.assertEqual(("library", None), (library_job.scope, library_job.project_id))
            self.assertIsNone(session.get(WorkflowSystemState, "workflow_ready"))

        accepted = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
        )
        self.assertTrue(accepted.ready)
        self.assertEqual(2, self._count(WorkflowStageRun))
        self.assertEqual(3, self._count(WorkflowArtifact))
        with self.sessions() as session:
            ready = session.get(WorkflowSystemState, "workflow_ready")
            self.assertEqual("ready", ready.value_json["status"])
        self.assertEqual([], self.migration.validate_migrated_workflows(self.sessions, accepted))

    def test_broken_legacy_dependency_rolls_back_source_and_leaves_readiness_unset(self) -> None:
        with closing(sqlite3.connect(self.ids["database_path"])) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO artifact_dependencies VALUES(?, ?, 'input')",
                (str(uuid.uuid4()), self.ids["artifact_alpha"]),
            )
            connection.commit()

        report = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
        )

        self.assertFalse(report.success)
        self.assertFalse(report.ready)
        self.assertTrue(any("artifact dependency" in error.lower() for error in report.errors))
        self.assertEqual(0, self._count(WorkflowStageRun))
        self.assertEqual(0, self._count(WorkflowArtifact))
        with self.sessions() as session:
            self.assertIsNotNone(session.get(WorkflowSystemState, "legacy_source_inventory"))
            self.assertIsNone(session.get(WorkflowSystemState, "workflow_ready"))

    def test_local_workspace_requires_explicit_owner_email(self) -> None:
        local_root = self.root / "local"
        create_legacy_fixture(local_root)

        with self.assertRaises(self.migration.WorkflowMigrationError):
            self.migration.migrate_legacy_workflows(
                local_root,
                self.backup_root,
                self.sessions,
                accept_missing_files=True,
            )

    def test_current_application_heartbeat_blocks_migration(self) -> None:
        with self.sessions.begin() as session:
            session.add(
                WorkflowSystemState(
                    key="application_heartbeat",
                    value_json={
                        "status": "running",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            )

        with self.assertRaises(self.migration.WorkflowMigrationError):
            self.migration.assert_application_stopped(self.sessions)

        with self.sessions.begin() as session:
            heartbeat = session.get(WorkflowSystemState, "application_heartbeat")
            heartbeat.value_json = {
                "status": "running",
                "observed_at": "2020-01-01T00:00:00+00:00",
            }
        self.migration.assert_application_stopped(self.sessions)

    def test_inventory_cli_writes_a_machine_readable_report(self) -> None:
        try:
            cli = importlib.import_module("review_writer_api.migrate_workflow")
        except ModuleNotFoundError as exc:
            raise AssertionError("The workflow migration maintenance CLI is missing.") from exc
        report_path = self.root / "reports" / "inventory.json"

        exit_code = cli.main(
            [
                "inventory",
                "--workspace-root",
                str(self.workspace_root),
                "--report",
                str(report_path),
            ]
        )

        self.assertEqual(0, exit_code)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["source_count"])
        self.assertEqual(3, payload["table_counts"]["artifact_versions"])

        with redirect_stderr(io.StringIO()) as stderr:
            blocked = cli.main(
                [
                    "migrate",
                    "--workspace-root",
                    str(self.workspace_root),
                    "--backup-root",
                    str(self.backup_root),
                    "--report",
                    str(self.root / "blocked.json"),
                ]
            )
        self.assertEqual(2, blocked)
        self.assertIn("--confirm-stopped", stderr.getvalue())


@unittest.skipUnless(
    os.environ.get("REVIEW_WRITER_RUN_POSTGRES_TESTS") == "1",
    "Set REVIEW_WRITER_RUN_POSTGRES_TESTS=1 for PostgreSQL migration tests.",
)
class PostgreSQLWorkflowMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = migration_api()
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary.name) / "hosted-workspaces"
        self.workspace_root.mkdir()
        self.sessions, self.engine = create_session_factory(database_url_from_env())
        marker = uuid.uuid4().hex
        with database_session(self.sessions) as session:
            user = User(
                email=f"migration-{marker}@example.com",
                display_name="Migration test",
                password_hash="hash",
            )
            session.add(user)
            session.flush()
            self.user_id = str(user.id)
            session.add_all(
                [
                    Project(user_id=user.id, slug="alpha", topic="Alpha"),
                    Project(user_id=user.id, slug="beta", topic="Beta"),
                ]
            )
        self.review_root = self.workspace_root / self.user_id
        self.ids = create_legacy_fixture(self.review_root)
        self.backup_root = Path(self.temporary.name) / "backups"

    def tearDown(self) -> None:
        with database_session(self.sessions) as session:
            session.execute(
                delete(WorkflowMigration).where(
                    WorkflowMigration.source_identity == self.ids["database_path"]
                )
            )
            session.execute(
                delete(WorkflowSystemState).where(
                    WorkflowSystemState.key.in_(
                        ["legacy_source_inventory", "workflow_ready"]
                    )
                )
            )
            user = session.get(User, uuid.UUID(self.user_id))
            if user is not None:
                session.delete(user)
        self.engine.dispose()
        self.temporary.cleanup()

    def test_postgresql_import_is_idempotent_and_preserves_foreign_keys(self) -> None:
        first = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
        )
        second = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
        )

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        with database_session(self.sessions) as session:
            self.assertEqual(
                2,
                session.scalar(
                    select(func.count())
                    .select_from(WorkflowStageRun)
                    .where(WorkflowStageRun.requested_by_user_id == uuid.UUID(self.user_id))
                ),
            )
            self.assertEqual(
                3,
                session.scalar(
                    select(func.count())
                    .select_from(WorkflowArtifact)
                    .join(Project, Project.id == WorkflowArtifact.project_id)
                    .where(Project.user_id == uuid.UUID(self.user_id))
                ),
            )


if __name__ == "__main__":
    unittest.main()
