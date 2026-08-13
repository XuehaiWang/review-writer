from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import review_writer_api.workflow_models  # noqa: F401 - registers target metadata
from review_writer_api.database import Project, User
from review_writer_api.workflow_models import WorkflowJob


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
}


class WorkflowMigrationTests(unittest.TestCase):
    def alembic_config(self) -> Config:
        return Config(str(ROOT / "alembic.ini"))

    def test_workflow_schema_has_separate_workflow_and_job_scope_revisions(self) -> None:
        script = ScriptDirectory.from_config(self.alembic_config())

        self.assertEqual(["20260813_0003"], script.get_heads())
        workflow_revision = script.get_revision("20260813_0002")
        self.assertEqual("20260811_0001", workflow_revision.down_revision)
        job_scope_revision = script.get_revision("20260813_0003")
        self.assertEqual("20260813_0002", job_scope_revision.down_revision)

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
