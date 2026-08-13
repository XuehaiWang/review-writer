from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.database import Base, Project, User
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.workflow_models import WorkflowArtifact
from review_writer_api.workflow_repository import WorkflowRepository
from review_writer_api.workspaces import HostedWorkspaceManager


def artifact_service_class():
    try:
        from review_writer_api.artifact_service import ArtifactService
    except ModuleNotFoundError as exc:
        raise AssertionError("The immutable artifact service is missing.") from exc
    return ArtifactService


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


class ArtifactServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions, self.engine = session_factory()
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace_manager = HostedWorkspaceManager(Path(self.temporary.name) / "users")
        with self.sessions.begin() as session:
            user = User(email="artifact@example.com", display_name="Artifact", password_hash="hash")
            session.add(user)
            session.flush()
            project = Project(user_id=user.id, slug="copper", topic="Copper")
            session.add(project)
            session.flush()
            self.user_id = str(user.id)
            self.project_id = str(project.id)
        self.repository = WorkflowRepository(self.sessions)
        self.service = artifact_service_class()(self.repository, self.workspace_manager)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def _stage(self, content: bytes, filename: str = "F001.svg"):
        run = self.repository.create_stage_run(
            self.user_id, self.project_id, "figures", status="running"
        )
        directory = self.service.stage_run_directory(
            self.user_id, self.project_id, run.id
        )
        source = directory / filename
        source.write_bytes(content)
        return run, source

    def test_publication_creates_immutable_versions_and_updates_current_after_validation(self) -> None:
        first_run, _first_source = self._stage(b"<svg>first</svg>")
        first = self.service.publish(
            self.user_id,
            self.project_id,
            first_run.id,
            "F001.svg",
            logical_name="figures/F001.svg",
            artifact_type="svg",
            producer_stage="figures",
            validator=lambda path: self.assertTrue(path.read_bytes().startswith(b"<svg")),
        )
        second_run, _second_source = self._stage(b"<svg>second</svg>")
        second = self.service.publish(
            self.user_id,
            self.project_id,
            second_run.id,
            "F001.svg",
            logical_name="figures/F001.svg",
            artifact_type="svg",
            producer_stage="figures",
        )

        first_path = self.workspace_manager.project_path(self.user_id, "copper") / first.relative_path
        second_path = self.workspace_manager.project_path(self.user_id, "copper") / second.relative_path
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(b"<svg>first</svg>", first_path.read_bytes())
        self.assertEqual(b"<svg>second</svg>", second_path.read_bytes())
        self.assertEqual(
            second.id,
            self.repository.get_current_artifact(
                self.user_id, self.project_id, "figures/F001.svg"
            ).id,
        )
        self.assertEqual(
            (".artifacts", "figures", "F001.svg", second.id, "F001.svg"),
            tuple(Path(second.relative_path).parts),
        )

    def test_validation_failure_and_path_escape_leave_database_and_current_unchanged(self) -> None:
        run, _source = self._stage(b"invalid")
        invalid_inputs = (
            {"staged_relative_path": "../outside.svg", "logical_name": "figures/F001.svg"},
            {"staged_relative_path": "F001.svg", "logical_name": "../outside.svg"},
            {"staged_relative_path": str(Path(self.temporary.name) / "outside.svg"), "logical_name": "figures/F001.svg"},
        )
        for values in invalid_inputs:
            with self.subTest(values=values), self.assertRaises(WorkflowValidationError):
                self.service.publish(
                    self.user_id,
                    self.project_id,
                    run.id,
                    values["staged_relative_path"],
                    logical_name=values["logical_name"],
                    artifact_type="svg",
                    producer_stage="figures",
                )

        with self.assertRaises(WorkflowValidationError):
            self.service.publish(
                self.user_id,
                self.project_id,
                run.id,
                "F001.svg",
                logical_name="figures/F001.svg",
                artifact_type="svg",
                producer_stage="figures",
                validator=lambda _path: (_ for _ in ()).throw(ValueError("invalid SVG")),
            )
        with self.sessions() as session:
            self.assertEqual(0, session.scalar(select(func.count()).select_from(WorkflowArtifact)))
        self.assertIsNone(
            self.repository.get_current_artifact(
                self.user_id, self.project_id, "figures/F001.svg"
            )
        )

    def test_database_failure_leaves_immutable_orphan_but_no_database_artifact(self) -> None:
        run, _source = self._stage(b"<svg>orphan</svg>")
        original_publish = self.repository.publish_artifact

        def fail_commit(**_kwargs):
            raise RuntimeError("simulated commit failure")

        self.repository.publish_artifact = fail_commit
        try:
            with self.assertRaises(RuntimeError):
                self.service.publish(
                    self.user_id,
                    self.project_id,
                    run.id,
                    "F001.svg",
                    logical_name="figures/F001.svg",
                    artifact_type="svg",
                    producer_stage="figures",
                )
        finally:
            self.repository.publish_artifact = original_publish

        project_root = self.workspace_manager.project_path(self.user_id, "copper")
        orphan_files = [
            path for path in project_root.glob(".artifacts/**/F001.svg") if path.is_file()
        ]
        self.assertEqual(1, len(orphan_files))
        with self.sessions() as session:
            self.assertEqual(0, session.scalar(select(func.count()).select_from(WorkflowArtifact)))

    def test_stage_run_and_project_must_belong_to_authenticated_user(self) -> None:
        run, _source = self._stage(b"<svg />")
        with self.sessions.begin() as session:
            other = User(email="other@example.com", display_name="Other", password_hash="hash")
            session.add(other)
            session.flush()
            other_id = str(other.id)

        with self.assertRaises(Exception):
            self.service.stage_run_directory(other_id, self.project_id, run.id)


if __name__ == "__main__":
    unittest.main()
