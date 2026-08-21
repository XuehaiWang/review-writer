from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import review_writer_api.workflow_models  # noqa: F401 - registers target metadata
from review_writer_api.database import Project, User
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper, WorkflowJob


ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_TABLES = {"users", "user_sessions", "projects", "provider_credentials"}
WORKFLOW_TABLES = {
    "workflow_system_state",
    "workflow_stage_runs",
    "workflow_stage_states",
    "workflow_artifacts",
    "workflow_current_artifacts",
    "workflow_artifact_dependencies",
    "workflow_jobs",
    "workflow_current_jobs",
    "workflow_approvals",
    "workflow_migrations",
    "library_papers",
    "library_bibliography_audits",
    "library_artifacts",
    "library_document_indexes",
    "library_document_chunks",
    "user_credit_accounts",
    "credit_reservations",
    "credit_transactions",
}


class WorkflowMigrationTests(unittest.TestCase):
    def alembic_config(self) -> Config:
        return Config(str(ROOT / "alembic.ini"))

    def test_workflow_schema_has_separate_workflow_and_job_scope_revisions(self) -> None:
        script = ScriptDirectory.from_config(self.alembic_config())

        self.assertEqual(["20260821_0015"], script.get_heads())
        workflow_revision = script.get_revision("20260813_0002")
        self.assertEqual("20260811_0001", workflow_revision.down_revision)
        job_scope_revision = script.get_revision("20260813_0003")
        self.assertEqual("20260813_0002", job_scope_revision.down_revision)
        library_revision = script.get_revision("20260813_0004")
        self.assertEqual("20260813_0003", library_revision.down_revision)
        library_artifact_revision = script.get_revision("20260813_0005")
        self.assertEqual("20260813_0004", library_artifact_revision.down_revision)
        artifact_lineage_revision = script.get_revision("20260813_0006")
        self.assertEqual("20260813_0005", artifact_lineage_revision.down_revision)
        mineru_artifact_revision = script.get_revision("20260814_0007")
        self.assertEqual("20260813_0006", mineru_artifact_revision.down_revision)
        model_gateway_revision = script.get_revision("20260818_0008")
        self.assertEqual("20260814_0007", model_gateway_revision.down_revision)
        image_gateway_revision = script.get_revision("20260818_0009")
        self.assertEqual("20260818_0008", image_gateway_revision.down_revision)
        mineru_usage_revision = script.get_revision("20260818_0010")
        self.assertEqual("20260818_0009", mineru_usage_revision.down_revision)
        document_index_revision = script.get_revision("20260819_0012")
        self.assertEqual("20260818_0011", document_index_revision.down_revision)
        bibliography_audit_revision = script.get_revision("20260820_0013")
        self.assertEqual("20260819_0012", bibliography_audit_revision.down_revision)
        credit_billing_revision = script.get_revision("20260820_0014")
        self.assertEqual("20260820_0013", credit_billing_revision.down_revision)

    def test_foundation_and_workflow_revisions_upgrade_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "migration.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
            environment = {"REVIEW_WRITER_DATABASE_URL": database_url}
            config = self.alembic_config()

            with patch.dict(os.environ, environment, clear=False):
                command.upgrade(config, "20260811_0001")
                engine = create_engine(database_url)
                foundation_tables = set(inspect(engine).get_table_names())
                engine.dispose()

                self.assertTrue(FOUNDATION_TABLES.issubset(foundation_tables))
                self.assertTrue(WORKFLOW_TABLES.isdisjoint(foundation_tables))

                command.upgrade(config, "head")
                engine = create_engine(database_url)
                all_tables = set(inspect(engine).get_table_names())
                engine.dispose()

                self.assertTrue(FOUNDATION_TABLES.issubset(all_tables))
                self.assertTrue(WORKFLOW_TABLES.issubset(all_tables))
                command.check(config)

    def test_0005_backfills_existing_catalog_into_immutable_library_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            database_path = temporary / "backfill.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
            workspace_root = temporary / "hosted-workspaces"
            config = self.alembic_config()
            environment = {
                "REVIEW_WRITER_DATABASE_URL": database_url,
                "REVIEW_WRITER_HOSTED_WORKSPACE_ROOT": str(workspace_root),
            }
            with patch.dict(os.environ, environment, clear=False):
                command.upgrade(config, "20260813_0004")
                engine = create_engine(database_url)
                sessions = sessionmaker(bind=engine, expire_on_commit=False)
                user_id = uuid.uuid4()
                paper_id = "P004"
                user_root = workspace_root / str(user_id)
                pdf = user_root / "review-library" / "uploads" / f"{paper_id}.pdf"
                markdown = (
                    user_root / "review-library" / "markdown" / f"{paper_id}.md"
                )
                compatibility = (
                    user_root
                    / "review-library"
                    / "metadata"
                    / "papers"
                    / f"{paper_id}.metadata.json"
                )
                for parent in (pdf.parent, markdown.parent, compatibility.parent):
                    parent.mkdir(parents=True, exist_ok=True)
                pdf.write_bytes(b"%PDF-1.7\nlegacy\n%%EOF\n")
                markdown.write_text("# Legacy markdown\n", encoding="utf-8")
                metadata = {
                    "paper_id": paper_id,
                    "title": {"value": "Legacy catalog paper"},
                    "source_paths": {
                        "pdf": str(pdf),
                        "markdown": str(markdown),
                    },
                }
                compatibility.write_text(json.dumps(metadata), encoding="utf-8")
                with sessions.begin() as session:
                    session.add(
                        User(
                            id=user_id,
                            email="library-backfill@example.com",
                            display_name="Library backfill",
                            password_hash="hash",
                        )
                    )
                    session.add(
                        LibraryPaper(
                            user_id=user_id,
                            paper_id=paper_id,
                            content_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                            original_filename=pdf.name,
                            title="Legacy catalog paper",
                            authors_json=[],
                            keywords_json=[],
                            tags_json={},
                            metadata_json=metadata,
                            pdf_relative_path=pdf.relative_to(user_root).as_posix(),
                            markdown_relative_path=markdown.relative_to(
                                user_root
                            ).as_posix(),
                            status="active",
                        )
                    )

                command.upgrade(config, "head")
                with sessions() as session:
                    paper = session.query(LibraryPaper).one()
                    artifacts = session.query(LibraryArtifact).all()

                self.assertEqual(3, len(artifacts))
                self.assertEqual(
                    {"pdf", "markdown", "metadata"},
                    {artifact.kind for artifact in artifacts},
                )
                self.assertEqual(
                    {str(artifact.id) for artifact in artifacts},
                    set(paper.metadata_json["_artifact_ids"].values()),
                )
                self.assertIn("review-library/.artifacts/", paper.pdf_relative_path)
                self.assertIn(
                    "review-library/.artifacts/", paper.markdown_relative_path
                )
                metadata_artifact = next(
                    artifact for artifact in artifacts if artifact.kind == "metadata"
                )
                immutable_metadata = (
                    user_root / Path(*metadata_artifact.relative_path.split("/"))
                )
                original_bytes = immutable_metadata.read_bytes()
                original_digest = hashlib.sha256(original_bytes).hexdigest()
                compatibility.write_text(
                    json.dumps({"paper_id": paper_id, "title": "edited"}),
                    encoding="utf-8",
                )
                self.assertEqual(original_bytes, immutable_metadata.read_bytes())
                self.assertEqual(original_digest, metadata_artifact.content_sha256)
                engine.dispose()

    def test_0005_backfill_rejects_intermediate_library_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            database_path = temporary / "unsafe-backfill.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
            workspace_root = temporary / "hosted-workspaces"
            config = self.alembic_config()
            environment = {
                "REVIEW_WRITER_DATABASE_URL": database_url,
                "REVIEW_WRITER_HOSTED_WORKSPACE_ROOT": str(workspace_root),
            }
            engine = None
            try:
                with patch.dict(os.environ, environment, clear=False):
                    command.upgrade(config, "20260813_0004")
                    engine = create_engine(database_url)
                    sessions = sessionmaker(bind=engine, expire_on_commit=False)
                    user_id = uuid.uuid4()
                    paper_id = "P005"
                    user_root = workspace_root / str(user_id)
                    review_library = user_root / "review-library"
                    pdf = review_library / "uploads" / f"{paper_id}.pdf"
                    markdown = review_library / "markdown" / f"{paper_id}.md"
                    pdf.parent.mkdir(parents=True, exist_ok=True)
                    markdown.parent.mkdir(parents=True, exist_ok=True)
                    pdf.write_bytes(b"%PDF-1.7\nunsafe\n%%EOF\n")
                    markdown.write_text("# Unsafe\n", encoding="utf-8")
                    with sessions.begin() as session:
                        session.add(
                            User(
                                id=user_id,
                                email="unsafe-backfill@example.com",
                                display_name="Unsafe backfill",
                                password_hash="hash",
                            )
                        )
                        session.add(
                            LibraryPaper(
                                user_id=user_id,
                                paper_id=paper_id,
                                content_sha256=hashlib.sha256(
                                    pdf.read_bytes()
                                ).hexdigest(),
                                original_filename=pdf.name,
                                title="Unsafe backfill",
                                authors_json=[],
                                keywords_json=[],
                                tags_json={},
                                metadata_json={"paper_id": paper_id},
                                pdf_relative_path=pdf.relative_to(
                                    user_root
                                ).as_posix(),
                                markdown_relative_path=markdown.relative_to(
                                    user_root
                                ).as_posix(),
                                status="active",
                            )
                        )
                    original_is_symlink = Path.is_symlink

                    def reports_intermediate_symlink(path: Path) -> bool:
                        return path == review_library or original_is_symlink(path)

                    with patch.object(
                        Path, "is_symlink", reports_intermediate_symlink
                    ):
                        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                            command.upgrade(config, "head")
            finally:
                if engine is not None:
                    engine.dispose()

    def test_job_scope_revision_downgrade_preserves_cross_project_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "downgrade.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
            config = self.alembic_config()
            with patch.dict(
                os.environ, {"REVIEW_WRITER_DATABASE_URL": database_url}, clear=False
            ):
                command.upgrade(config, "head")
                engine = create_engine(database_url)
                sessions = sessionmaker(bind=engine, expire_on_commit=False)
                with sessions.begin() as session:
                    user = User(
                        email="downgrade@example.com",
                        display_name="Downgrade",
                        password_hash="hash",
                    )
                    session.add(user)
                    session.flush()
                    first = Project(user_id=user.id, slug="first", topic="First")
                    second = Project(user_id=user.id, slug="second", topic="Second")
                    session.add_all([first, second])
                    session.flush()
                    session.add_all(
                        [
                            WorkflowJob(
                                user_id=user.id,
                                project_id=first.id,
                                scope="project",
                                job_type="discovery",
                                idempotency_scope_key=str(first.id),
                                idempotency_key="same-request",
                            ),
                            WorkflowJob(
                                user_id=user.id,
                                project_id=second.id,
                                scope="project",
                                job_type="discovery",
                                idempotency_scope_key=str(second.id),
                                idempotency_key="same-request",
                            ),
                        ]
                    )

                command.downgrade(config, "20260813_0002")
                with engine.connect() as connection:
                    keys = connection.execute(
                        text(
                            "SELECT idempotency_key FROM workflow_jobs "
                            "ORDER BY idempotency_key"
                        )
                    ).scalars().all()
                constraints = inspect(engine).get_unique_constraints("workflow_jobs")
                engine.dispose()

        self.assertEqual(2, len(keys))
        self.assertEqual(2, len(set(keys)))
        self.assertIn("same-request", keys)
        self.assertIn(
            "uq_workflow_job_user_idempotency",
            {constraint["name"] for constraint in constraints},
        )


if __name__ == "__main__":
    unittest.main()
