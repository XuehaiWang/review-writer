from __future__ import annotations

import base64
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import WorkflowStageState


TEST_CREDENTIAL_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


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


class TaxonomyProfileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions, self.engine = session_factory()
        with self.sessions.begin() as session:
            user = User(email="taxonomy@example.com", display_name="Taxonomy", password_hash="hash")
            session.add(user)
            session.flush()
            self.user_id = str(user.id)
            self.principal = Principal(
                user_id=self.user_id,
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
        self.app = create_app(
            settings,
            principal_provider=lambda: self.principal,
            session_factory_override=self.sessions,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _create_project(self, client: TestClient, slug: str, **payload):
        response = client.post(
            "/api/v1/projects",
            json={"slug": slug, "topic": "General research topic", **payload},
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def _seed_states(self, project_id: str, states: dict[str, str]) -> None:
        with self.sessions.begin() as session:
            project = session.get(Project, uuid.UUID(project_id))
            self.assertIsNotNone(project)
            stored = {}
            for index, (stage_id, status) in enumerate(states.items(), start=1):
                session.add(
                    WorkflowStageState(
                        project_id=project.id,
                        stage_id=stage_id,
                        status=status,
                        revision=index,
                    )
                )
                stored[stage_id] = {"status": status, "revision": index}
            project.stage_states = stored
            project.current_stage = next(iter(states), "discovery")

    def test_catalog_and_new_project_default_to_general_academic(self) -> None:
        with TestClient(self.app) as client:
            catalog = client.get("/api/v1/taxonomy-profiles")
            self.assertEqual(200, catalog.status_code)
            payload = catalog.json()
            self.assertEqual("general_academic", payload["default_profile"])
            profiles = {item["id"]: item for item in payload["items"]}
            self.assertEqual({"general_academic", "chemistry_general"}, set(profiles))
            self.assertFalse(profiles["general_academic"]["domain_rules_enabled"])
            self.assertTrue(profiles["chemistry_general"]["domain_rules_enabled"])

            project = self._create_project(client, "general-default")
            self.assertEqual("general_academic", project["taxonomy_profile"])

            internal_profile = client.post(
                "/api/v1/projects",
                json={
                    "slug": "internal-profile",
                    "topic": "Axially chiral allene synthesis",
                    "taxonomy_profile": "allene",
                },
            )
            self.assertEqual(409, internal_profile.status_code, internal_profile.text)

    def test_profile_change_before_matrix_leaves_downstream_state_unchanged(self) -> None:
        with TestClient(self.app) as client:
            project = self._create_project(client, "before-matrix")
            self._seed_states(
                project["project_id"],
                {"discovery": "review", "sections": "approved"},
            )

            response = client.patch(
                f"/api/v1/projects/{project['project_id']}/taxonomy-profile",
                json={"taxonomy_profile": "chemistry_general"},
            )
            self.assertEqual(200, response.status_code, response.text)
            result = response.json()
            self.assertTrue(result["changed"])
            self.assertFalse(result["matrix_entered"])
            self.assertFalse(result["downstream_stale"])

            with self.sessions() as session:
                rows = {
                    row.stage_id: row.status
                    for row in session.scalars(
                        select(WorkflowStageState).where(
                            WorkflowStageState.project_id == uuid.UUID(project["project_id"])
                        )
                    )
                }
            self.assertEqual("stale", rows["discovery"])
            self.assertEqual("approved", rows["sections"])

    def test_profile_change_after_matrix_requires_confirmation_and_stales_descendants(self) -> None:
        with TestClient(self.app) as client:
            project = self._create_project(
                client, "after-matrix", taxonomy_profile="chemistry_general"
            )
            self._seed_states(
                project["project_id"],
                {"discovery": "approved", "matrix": "review", "blueprint": "approved"},
            )
            path = f"/api/v1/projects/{project['project_id']}/taxonomy-profile"

            blocked = client.patch(path, json={"taxonomy_profile": "general_academic"})
            self.assertEqual(409, blocked.status_code)
            self.assertIn("confirm_downstream_invalidation", blocked.json()["detail"])

            changed = client.patch(
                path,
                json={
                    "taxonomy_profile": "general_academic",
                    "confirm_downstream_invalidation": True,
                },
            )
            self.assertEqual(200, changed.status_code, changed.text)
            result = changed.json()
            self.assertTrue(result["matrix_entered"])
            self.assertTrue(result["downstream_stale"])
            self.assertEqual("general_academic", result["project"]["taxonomy_profile"])

            with self.sessions() as session:
                rows = {
                    row.stage_id: row.status
                    for row in session.scalars(
                        select(WorkflowStageState).where(
                            WorkflowStageState.project_id == uuid.UUID(project["project_id"])
                        )
                    )
                }
            self.assertEqual(
                {"discovery": "stale", "matrix": "stale", "blueprint": "stale"},
                rows,
            )


if __name__ == "__main__":
    unittest.main()
