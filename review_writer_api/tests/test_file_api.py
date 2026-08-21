from __future__ import annotations

import base64
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.security import Principal, Role


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


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


class ArtifactFileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions, self.engine = session_factory()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = ApiSettings(
            review_root=root,
            deployment_mode="hosted",
            database_url="sqlite+pysqlite:///:memory:",
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=root / "users",
        )
        with self.sessions.begin() as session:
            first = User(email="first@example.com", display_name="First", password_hash="hash")
            second = User(email="second@example.com", display_name="Second", password_hash="hash")
            session.add_all([first, second])
            session.flush()
            project = Project(user_id=first.id, slug="copper", topic="Copper")
            session.add(project)
            session.flush()
            self.first = Principal(
                user_id=str(first.id), roles=frozenset({Role.USER}), email=first.email
            )
            self.second = Principal(
                user_id=str(second.id), roles=frozenset({Role.USER}), email=second.email
            )
            self.project_id = str(project.id)
        self.current_principal = self.first
        self.app = create_app(
            self.settings,
            principal_provider=lambda: self.current_principal,
            session_factory_override=self.sessions,
        )
        repository = self.app.state.workflow_repository
        service = self.app.state.artifact_service
        run = repository.create_stage_run(
            self.first.user_id, self.project_id, "figures", status="running"
        )
        staging = service.stage_run_directory(self.first.user_id, self.project_id, run.id)
        content = bytes(range(256)) * 8
        (staging / "figure.bin").write_bytes(content)
        self.content = content
        self.artifact = service.publish(
            self.first.user_id,
            self.project_id,
            run.id,
            "figure.bin",
            logical_name="figures/figure.bin",
            artifact_type="bin",
            producer_stage="figures",
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def test_complete_file_uses_streaming_file_response_and_safe_headers(self) -> None:
        with TestClient(self.app) as client, patch.object(
            Path, "read_bytes", side_effect=AssertionError("must not buffer with read_bytes")
        ):
            response = client.get(f"/api/v1/artifacts/{self.artifact.id}/content")

        self.assertEqual(200, response.status_code)
        self.assertEqual(self.content, response.content)
        self.assertEqual("bytes", response.headers["accept-ranges"])
        self.assertEqual(f'"{self.artifact.content_sha256}"', response.headers["etag"])
        self.assertIn("figure.bin", response.headers["content-disposition"])
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))

    def test_single_byte_range_returns_bounded_206_response(self) -> None:
        with TestClient(self.app) as client:
            response = client.get(
                f"/api/v1/artifacts/{self.artifact.id}/content",
                headers={"Range": "bytes=0-99"},
            )

        self.assertEqual(206, response.status_code)
        self.assertEqual(self.content[:100], response.content)
        self.assertEqual("100", response.headers["content-length"])
        self.assertEqual(
            f"bytes 0-99/{len(self.content)}", response.headers["content-range"]
        )
        self.assertEqual("bytes", response.headers["accept-ranges"])
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))

    def test_unsatisfiable_range_reports_complete_artifact_size(self) -> None:
        with TestClient(self.app) as client:
            response = client.get(
                f"/api/v1/artifacts/{self.artifact.id}/content",
                headers={"Range": f"bytes={len(self.content)}-"},
            )

        self.assertEqual(416, response.status_code)
        self.assertEqual(
            "ARTIFACT_RANGE_NOT_SATISFIABLE", response.json()["error"]["code"]
        )
        self.assertEqual(
            f"bytes */{len(self.content)}", response.headers["content-range"]
        )

    def test_cross_user_and_unauthenticated_reads_do_not_expose_artifact(self) -> None:
        self.current_principal = self.second
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/artifacts/{self.artifact.id}/content")
        self.assertEqual(404, response.status_code)
        self.assertEqual("WORKFLOW_NOT_FOUND", response.json()["error"]["code"])

        unauthenticated_app = create_app(
            self.settings,
            session_factory_override=self.sessions,
        )
        with TestClient(unauthenticated_app) as client:
            unauthenticated = client.get(
                f"/api/v1/artifacts/{self.artifact.id}/content"
            )
        self.assertEqual(401, unauthenticated.status_code)

    def test_missing_file_returns_stable_error(self) -> None:
        resolved = self.app.state.artifact_service.resolve_owned_artifact(
            self.first.user_id, self.artifact.id
        )
        resolved.path.unlink()

        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/artifacts/{self.artifact.id}/content")

        self.assertEqual(404, response.status_code)
        self.assertEqual("ARTIFACT_FILE_MISSING", response.json()["error"]["code"])

    def test_project_delete_soft_deletes_then_atomically_moves_files_to_user_trash(self) -> None:
        project_path = self.app.state.hosted_workspace_manager.project_path(
            self.first.user_id, "copper"
        )
        self.assertTrue(project_path.is_dir())

        with TestClient(self.app) as client:
            deleted = client.delete(
                f"/api/v1/projects/{self.project_id}",
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(204, deleted.status_code)
        with self.sessions() as session:
            project = session.get(Project, uuid.UUID(self.project_id))
            self.assertIsNotNone(project.deleted_at)
        self.assertFalse(project_path.exists())
        trash_root = (
            self.app.state.hosted_workspace_manager.user_root(self.first.user_id) / ".trash"
        )
        trashed_projects = list(trash_root.glob("copper-*"))
        self.assertEqual(1, len(trashed_projects))
        self.assertTrue(any(path.is_file() for path in trashed_projects[0].rglob("*")))

        with TestClient(self.app) as client:
            unavailable = client.get(
                f"/api/v1/artifacts/{self.artifact.id}/content"
            )
        self.assertEqual(404, unavailable.status_code)

    def test_project_delete_restores_database_state_when_trash_move_fails(self) -> None:
        project_path = self.app.state.hosted_workspace_manager.project_path(
            self.first.user_id, "copper"
        )
        with patch.object(
            self.app.state.artifact_service,
            "trash_project",
            side_effect=OSError("simulated move failure"),
        ), TestClient(self.app, raise_server_exceptions=False) as client:
            failed = client.delete(
                f"/api/v1/projects/{self.project_id}",
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(500, failed.status_code)
        self.assertEqual("PROJECT_ARCHIVE_FAILED", failed.json()["error"]["code"])
        self.assertTrue(failed.json()["error"]["retryable"])
        with self.sessions() as session:
            project = session.get(Project, uuid.UUID(self.project_id))
            self.assertIsNone(project.deleted_at)
            self.assertEqual("active", project.status)
        self.assertTrue(project_path.is_dir())

        with TestClient(self.app) as client:
            retried = client.delete(
                f"/api/v1/projects/{self.project_id}",
                headers={"Origin": "http://testserver"},
            )
        self.assertEqual(204, retried.status_code)

    def test_deleted_project_slug_can_be_reused_as_a_clean_project(self) -> None:
        with TestClient(self.app) as client:
            deleted = client.delete(
                f"/api/v1/projects/{self.project_id}",
                headers={"Origin": "http://testserver"},
            )
            recreated = client.post(
                "/api/v1/projects",
                json={
                    "slug": "copper",
                    "topic": "A new copper review",
                    "taxonomy_profile": "general_academic",
                    "model_tier": "terra",
                },
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(204, deleted.status_code)
        self.assertEqual(201, recreated.status_code, recreated.text)
        replacement_id = recreated.json()["project_id"]
        self.assertNotEqual(self.project_id, replacement_id)
        self.assertEqual("copper", recreated.json()["slug"])

        with self.sessions() as session:
            self.assertIsNone(session.get(Project, uuid.UUID(self.project_id)))
            replacement = session.get(Project, uuid.UUID(replacement_id))
            self.assertIsNotNone(replacement)
            self.assertIsNone(replacement.deleted_at)
            self.assertEqual("active", replacement.status)


if __name__ == "__main__":
    unittest.main()
