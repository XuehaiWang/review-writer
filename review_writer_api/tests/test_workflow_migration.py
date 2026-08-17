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
from unittest.mock import Mock, patch

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
from review_writer_api.domain_services.library import LibraryService
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import (
    LibraryArtifact,
    LibraryPaper,
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
from review_writer_api.workspaces import HostedWorkspaceManager
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


def create_legacy_schema(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE stage_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                stage_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                input_fingerprint TEXT NOT NULL DEFAULT '',
                input_snapshot_json TEXT NOT NULL DEFAULT '[]',
                output_fingerprint TEXT NOT NULL DEFAULT '',
                output_snapshot_json TEXT NOT NULL DEFAULT '[]',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE stage_state (
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                stage_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_run_id TEXT,
                input_fingerprint TEXT NOT NULL DEFAULT '',
                output_fingerprint TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, stage_id)
            );
            CREATE TABLE artifact_versions (
                artifact_version_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                logical_name TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                producer_stage TEXT NOT NULL DEFAULT '',
                producer_run_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(project_id, logical_name, content_sha256)
            );
            CREATE TABLE current_artifacts (
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                logical_name TEXT NOT NULL,
                artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(artifact_version_id) ON DELETE CASCADE,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, logical_name)
            );
            CREATE TABLE artifact_dependencies (
                output_artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(artifact_version_id) ON DELETE CASCADE,
                input_artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(artifact_version_id) ON DELETE CASCADE,
                dependency_role TEXT NOT NULL DEFAULT 'input',
                PRIMARY KEY(output_artifact_version_id, input_artifact_version_id, dependency_role)
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE current_jobs (
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                job_type TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, job_type)
            );
            """
        )


def create_legacy_fixture(review_root: Path) -> dict[str, str]:
    database_path = review_root / ".review-writer" / "workflow.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    create_legacy_schema(database_path)
    alpha_root = review_root / "review-projects" / "alpha"
    beta_root = review_root / "review-projects" / "beta"
    alpha_root.mkdir(parents=True, exist_ok=True)
    beta_root.mkdir(parents=True, exist_ok=True)
    shared_content = b'{"paper":"copper"}\n'
    alpha_file = alpha_root / "artifact.json"
    beta_file = beta_root / "duplicate.json"
    alpha_file.write_bytes(shared_content)
    beta_file.write_bytes(shared_content)
    library_pdf = review_root / "review-library" / "uploads" / "P001.pdf"
    library_markdown = review_root / "mineru-outputs" / "markdown" / "P001.md"
    library_extracted = review_root / "mineru-outputs" / "extracted" / "P001"
    library_content_list = library_extracted / "P001_content_list.json"
    library_figure = library_extracted / "images" / "figure-1.jpg"
    library_metadata = review_root / "review-library" / "metadata" / "papers" / "P001.metadata.json"
    library_pdf.parent.mkdir(parents=True, exist_ok=True)
    library_markdown.parent.mkdir(parents=True, exist_ok=True)
    library_metadata.parent.mkdir(parents=True, exist_ok=True)
    library_figure.parent.mkdir(parents=True, exist_ok=True)
    library_pdf.write_bytes(b"%PDF-1.7\nlegacy\n%%EOF")
    library_markdown.write_text("# Legacy copper paper\n", encoding="utf-8")
    library_figure.write_bytes(b"legacy-mineru-figure")
    library_content_list.write_text(
        json.dumps([{"type": "image", "img_path": "images/figure-1.jpg"}]),
        encoding="utf-8",
    )
    library_metadata.write_text(
        json.dumps(
            {
                "paper_id": "P001",
                "title": {"value": "Legacy copper paper"},
                "authors": {"value": ["Legacy Author"]},
                "keywords": {"value": ["copper"]},
                "structured_tags": {"value": {"reaction_type": "allenation"}},
                "source_file": {
                    "original_upload_name": "legacy.pdf",
                    "sha256": sha256(library_pdf.read_bytes()),
                },
                "source_paths": {
                    "pdf": str(library_pdf),
                    "markdown": str(library_markdown),
                    "extracted_dir": str(library_extracted),
                    "content_list": str(library_content_list),
                },
                "extraction": {
                    "inputs": {
                        "extracted_dir": str(library_extracted),
                        "content_list": str(library_content_list),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

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
        self.assertEqual(1, self._count(LibraryPaper))
        self.assertEqual(4, self._count(LibraryArtifact))
        self.assertEqual(1, self._count(WorkflowMigration))

        with self.sessions() as session:
            run = session.get(WorkflowStageRun, uuid.UUID(self.ids["run_alpha"]))
            self.assertEqual("zh", run.metadata_json["language"])
            paper = session.scalar(select(LibraryPaper))
            artifacts = list(session.scalars(select(LibraryArtifact)))
            self.assertEqual("P001", paper.paper_id)
            self.assertEqual(
                {"pdf", "markdown", "metadata", "mineru"},
                {artifact.kind for artifact in artifacts},
            )
            self.assertEqual(
                {str(artifact.id) for artifact in artifacts},
                set(paper.metadata_json["_artifact_ids"].values()),
            )
            self.assertEqual("Legacy copper paper", paper.title)
            self.assertIn("review-library/.artifacts/P001/", paper.pdf_relative_path)
            mineru = next(artifact for artifact in artifacts if artifact.kind == "mineru")
            self.assertIn("/extracted/", mineru.relative_path)
            self.assertTrue((self.review_root / mineru.relative_path).is_file())
            self.assertTrue(
                Path(paper.metadata_json["source_paths"]["extracted_dir"]).is_dir()
            )
            self.assertEqual(self.ids["run_alpha"], run.legacy_id)
            missing = session.get(WorkflowArtifact, uuid.UUID(self.ids["artifact_missing"]))
            self.assertEqual("missing", missing.availability)
            self.assertEqual("missing.png", missing.relative_path)
            alpha = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.legacy_id == self.ids["artifact_alpha"]
                )
            )
            self.assertTrue(alpha.relative_path.startswith(".artifacts/migrated/"))
            migrated_alpha = self.review_root / "review-projects" / "alpha" / alpha.relative_path
            self.assertEqual(shared_content := b'{"paper":"copper"}\n', migrated_alpha.read_bytes())
            self.assertEqual(sha256(shared_content), alpha.content_sha256)
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
        self.assertEqual(
            [], self.migration.validate_migrated_workflows(self.sessions, accepted)
        )

    def test_file_hash_drift_requires_separate_acknowledgement_and_preserves_both_hashes(self) -> None:
        changed = self.review_root / "review-projects" / "alpha" / "artifact.json"
        changed.write_bytes(b'{"paper":"changed after legacy publication"}\n')
        actual_sha256 = hashlib.sha256(changed.read_bytes()).hexdigest()

        blocked = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
            accept_file_drift=False,
        )

        self.assertTrue(blocked.success)
        self.assertFalse(blocked.ready)
        self.assertEqual(1, len(blocked.drifted_files))
        self.assertEqual(actual_sha256, blocked.drifted_files[0]["actual_sha256"])
        with self.sessions() as session:
            artifact = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.legacy_id == self.ids["artifact_alpha"]
                )
            )
            self.assertEqual("integrity_mismatch", artifact.availability)

        accepted = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
            accept_file_drift=True,
        )

        self.assertTrue(accepted.ready)
        with self.sessions() as session:
            artifact = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.legacy_id == self.ids["artifact_alpha"]
                )
            )
            self.assertEqual("available", artifact.availability)
            self.assertEqual(actual_sha256, artifact.content_sha256)
            self.assertEqual(
                sha256(b'{"paper":"copper"}\n'),
                artifact.metadata_json["legacy_content_sha256"],
            )
            self.assertEqual(actual_sha256, artifact.metadata_json["migrated_actual_sha256"])

    def test_container_absolute_library_and_external_artifact_paths_are_preserved(self) -> None:
        metadata_path = (
            self.review_root
            / "review-library"
            / "metadata"
            / "papers"
            / "P001.metadata.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        prefix = "/app/.review-writer/hosted-workspaces/legacy-user"
        metadata["source_paths"]["pdf"] = f"{prefix}/review-library/uploads/P001.pdf"
        metadata["source_paths"]["markdown"] = f"{prefix}/mineru-outputs/markdown/P001.md"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        external = self.review_root / "mineru-outputs" / "extracted" / "P999" / "images" / "figure.png"
        external.parent.mkdir(parents=True, exist_ok=True)
        external.write_bytes(b"legacy-external-image")
        external_id = str(uuid.uuid4())
        container_path = f"{prefix}/mineru-outputs/extracted/P999/images/figure.png"
        with closing(sqlite3.connect(self.ids["database_path"])) as connection:
            connection.execute(
                """
                INSERT INTO artifact_versions(
                    artifact_version_id, project_id, logical_name, artifact_type, path,
                    content_sha256, size_bytes, mtime_ns, producer_stage, producer_run_id,
                    metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    external_id,
                    "alpha",
                    container_path,
                    "png",
                    container_path,
                    sha256(external.read_bytes()),
                    external.stat().st_size,
                    external.stat().st_mtime_ns,
                    "figures",
                    None,
                    "{}",
                    NOW,
                ),
            )
            connection.commit()

        report = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=False,
        )

        self.assertEqual(1, len(report.missing_files))
        self.assertEqual("missing.png", report.missing_files[0]["logical_name"])
        with self.sessions() as session:
            artifact = session.scalar(
                select(WorkflowArtifact).where(WorkflowArtifact.legacy_id == external_id)
            )
            self.assertEqual("available", artifact.availability)
            self.assertTrue(artifact.relative_path.startswith(".artifacts/migrated/"))
            destination = (
                self.review_root / "review-projects" / "alpha" / artifact.relative_path
            )
            self.assertEqual(external.read_bytes(), destination.read_bytes())

    def test_migrated_library_metadata_edit_preserves_imported_artifact(self) -> None:
        report = self.migration.migrate_legacy_workflows(
            self.workspace_root,
            self.backup_root,
            self.sessions,
            accept_missing_files=True,
        )
        self.assertTrue(report.ready)
        with self.sessions() as session:
            paper = session.scalar(select(LibraryPaper))
            imported_metadata = session.get(
                LibraryArtifact,
                uuid.UUID(paper.metadata_json["_artifact_ids"]["metadata"]),
            )
            imported_path = self.review_root / Path(
                *imported_metadata.relative_path.split("/")
            )
            imported_bytes = imported_path.read_bytes()
            imported_sha256 = imported_metadata.content_sha256

        service = LibraryService(
            self.sessions, HostedWorkspaceManager(self.workspace_root)
        )
        principal = Principal(
            self.user_id, frozenset({Role.USER}), "owner@example.com"
        )
        updated_metadata = dict(paper.metadata_json)
        updated_metadata["title"] = {"value": "Edited after migration"}
        updated = service.update_metadata(principal, paper.paper_id, updated_metadata)

        self.assertEqual("Edited after migration", updated.title)
        self.assertNotEqual(
            str(imported_metadata.id), updated.artifact_ids["metadata"]
        )
        self.assertEqual(imported_bytes, imported_path.read_bytes())
        self.assertEqual(imported_sha256, hashlib.sha256(imported_bytes).hexdigest())

    def test_import_rejects_intermediate_library_symlink(self) -> None:
        unsafe_directory = self.review_root / "review-library"
        original_is_symlink = Path.is_symlink

        def reports_intermediate_symlink(path: Path) -> bool:
            return path == unsafe_directory or original_is_symlink(path)

        with patch.object(Path, "is_symlink", reports_intermediate_symlink):
            report = self.migration.migrate_legacy_workflows(
                self.workspace_root,
                self.backup_root,
                self.sessions,
                accept_missing_files=True,
            )

        self.assertFalse(report.success)
        self.assertTrue(
            any("symbolic link" in error.lower() for error in report.errors),
            report.errors,
        )
        self.assertEqual(0, self._count(LibraryPaper))
        self.assertEqual(0, self._count(LibraryArtifact))

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

    def test_formal_cli_returns_nonzero_until_missing_files_are_acknowledged(self) -> None:
        cli = importlib.import_module("review_writer_api.migrate_workflow")
        report_path = self.root / "reports" / "not-ready.json"
        disposable_engine = Mock()

        with patch.object(
            cli, "_database", return_value=(self.sessions, disposable_engine)
        ):
            exit_code = cli.main(
                [
                    "migrate",
                    "--workspace-root",
                    str(self.workspace_root),
                    "--backup-root",
                    str(self.backup_root),
                    "--report",
                    str(report_path),
                    "--confirm-stopped",
                ]
            )

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["success"])
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["missing_files"])
        self.assertEqual(2, exit_code)
        disposable_engine.dispose.assert_called_once_with()


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
