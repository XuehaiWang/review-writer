"""Contract tests for the shared helpers introduced while removing duplication.

Every assertion here pins behavior that used to be implemented in two or more
places, so a future edit cannot silently let the copies diverge again.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import Base, Project, User
from review_writer_api.domain_services.base import OwnedProjectService
from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowStageNotReady,
)
from review_writer_api.security import Principal, Role
from review_writer_api.tests._test_support import session_factory
from review_writer_api.workflow_migration import WorkflowMigrationError
from review_writer_api.workflow_repository import WorkflowRepository
from review_writer_api.workspaces import HostedWorkspaceManager


class _ReadJsonProbe(OwnedProjectService):
    """Minimal service that exposes the shared current-artifact JSON reader."""

    def __init__(self, repository, artifacts):
        self.repository = repository
        self.artifacts = artifacts


class _SharedProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions, self.engine = session_factory()
        self.temporary = tempfile.TemporaryDirectory()
        self.workspaces = HostedWorkspaceManager(Path(self.temporary.name) / "users")
        with self.sessions.begin() as session:
            user = User(
                email="shared@example.com", display_name="Shared", password_hash="hash"
            )
            session.add(user)
            session.flush()
            project = Project(user_id=user.id, slug="shared", topic="Shared")
            session.add(project)
            session.flush()
            self.user_id = str(user.id)
            self.project_id = str(project.id)
        self.repository = WorkflowRepository(self.sessions)
        self.artifacts = ArtifactService(self.repository, self.workspaces)
        self.principal = Principal(
            self.user_id, frozenset({Role.USER}), "shared@example.com"
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def publish_file(self, logical_name: str, content: bytes, *, stage: str = "matrix"):
        run = self.repository.create_stage_run(
            self.user_id, self.project_id, stage, status="running"
        )
        staging = self.artifacts.stage_run_directory(
            self.user_id, self.project_id, run.id
        )
        filename = logical_name.split("/")[-1]
        (staging / filename).write_bytes(content)
        return self.artifacts.publish(
            self.user_id,
            self.project_id,
            run.id,
            filename,
            logical_name=logical_name,
            artifact_type="json",
            producer_stage=stage,
        )


class ReadJsonOptionalContractTests(_SharedProjectCase):
    """`OwnedProjectService._read_json_optional` is the single JSON reader."""

    def setUp(self) -> None:
        super().setUp()
        self.reader = _ReadJsonProbe(self.repository, self.artifacts)

    def test_valid_mapping_returns_payload_and_current_artifact(self) -> None:
        artifact = self.publish_file(
            "planning/outline.json", b'{"headings": ["Intro"]}'
        )
        payload, current = self.reader._read_json_optional(
            self.principal, self.project_id, "planning/outline.json"
        )
        self.assertEqual({"headings": ["Intro"]}, payload)
        self.assertIsNotNone(current)
        self.assertEqual(artifact.id, current.id)

    def test_missing_required_artifact_raises_stage_not_ready(self) -> None:
        with self.assertRaises(WorkflowStageNotReady):
            self.reader._read_json_optional(
                self.principal, self.project_id, "planning/outline.json"
            )

    def test_missing_optional_artifact_returns_none_pair(self) -> None:
        payload, current = self.reader._read_json_optional(
            self.principal,
            self.project_id,
            "planning/outline.json",
            required=False,
        )
        self.assertIsNone(payload)
        self.assertIsNone(current)

    def test_unreadable_json_reports_the_caller_label(self) -> None:
        self.publish_file("planning/outline.json", b"{not valid json")
        with self.assertRaises(WorkflowConflict) as caught:
            self.reader._read_json_optional(
                self.principal,
                self.project_id,
                "planning/outline.json",
                artifact_label="Planning",
            )
        self.assertIn("Planning artifact is unreadable", str(caught.exception))

    def test_non_mapping_json_reports_the_caller_label(self) -> None:
        self.publish_file("planning/outline.json", b'["not", "a", "mapping"]')
        with self.assertRaises(WorkflowConflict) as caught:
            self.reader._read_json_optional(
                self.principal,
                self.project_id,
                "planning/outline.json",
                artifact_label="workflow",
            )
        self.assertIn("workflow artifact is invalid", str(caught.exception))

    def test_foreign_owner_cannot_read_the_artifact(self) -> None:
        self.publish_file("planning/outline.json", b"{}")
        outsider = Principal(
            "00000000-0000-0000-0000-000000000000",
            frozenset({Role.USER}),
            "outsider@example.com",
        )
        with self.assertRaises(WorkflowNotFound):
            self.reader._read_json_optional(
                outsider, self.project_id, "planning/outline.json"
            )


class ReadJsonDelegationContractTests(unittest.TestCase):
    """The stage services must delegate, not re-implement, the shared reader."""

    @staticmethod
    def _stub():
        sentinel = ({"ok": True}, object())

        class _Stub:
            def _read_json_optional(
                self,
                principal,
                project_id,
                logical_name,
                *,
                required=True,
                artifact_label="workflow",
            ):
                self.seen = (principal, project_id, logical_name, required, artifact_label)
                return sentinel

        return _Stub(), sentinel

    def test_planning_reader_delegates_with_the_planning_label(self) -> None:
        stub, sentinel = self._stub()
        self.assertIs(
            sentinel, PlanningService._read_json(stub, "P", "proj", "planning/outline.json")
        )
        self.assertEqual(
            ("P", "proj", "planning/outline.json", True, "Planning"), stub.seen
        )
        PlanningService._read_json(
            stub, "P", "proj", "planning/outline.json", required=False
        )
        self.assertEqual(
            ("P", "proj", "planning/outline.json", False, "Planning"), stub.seen
        )

    def test_sections_reader_delegates_with_the_default_workflow_label(self) -> None:
        stub, sentinel = self._stub()
        self.assertIs(
            sentinel,
            SectionsService._read_json_artifact(
                stub, "P", "proj", "sections/index.json", required=False
            ),
        )
        self.assertEqual(
            ("P", "proj", "sections/index.json", False, "workflow"), stub.seen
        )


class PromoteDiscoveryContractTests(_SharedProjectCase):
    """Both Discovery promotion entry points share one implementation."""

    def discovery_inputs(self, *, tag: str = "a"):
        run = self.repository.create_stage_run(
            self.user_id, self.project_id, "discovery", status="succeeded"
        )
        artifact = self.repository.create_artifact(
            self.user_id,
            self.project_id,
            logical_name="discovery/review.json",
            artifact_type="json",
            relative_path=f".artifacts/discovery/{tag}.json",
            content_sha256=tag * 64,
            size_bytes=2,
            mtime_ns=1,
            producer_stage="discovery",
            producer_run_id=run.id,
        )
        return run, artifact

    def test_replace_promotes_the_candidate_as_a_review_revision(self) -> None:
        run, artifact = self.discovery_inputs()
        state = self.repository.replace_discovery_atomically(
            self.user_id,
            self.project_id,
            artifact_id=artifact.id,
            run_id=run.id,
            expected_revision=0,
            topic="Copper",
        )
        self.assertEqual("review", state.status)
        self.assertEqual(1, state.revision)
        current = self.repository.get_current_artifact(
            self.user_id, self.project_id, "discovery/review.json"
        )
        self.assertIsNotNone(current)
        self.assertEqual(artifact.id, current.id)

    def test_save_promotes_the_requested_status(self) -> None:
        run, artifact = self.discovery_inputs()
        state = self.repository.save_discovery_atomically(
            self.user_id,
            self.project_id,
            artifact_id=artifact.id,
            run_id=run.id,
            expected_revision=0,
            status="completed",
        )
        self.assertEqual("completed", state.status)
        self.assertEqual(1, state.revision)

    def test_both_entries_share_the_stale_revision_guard(self) -> None:
        run, artifact = self.discovery_inputs()
        self.repository.replace_discovery_atomically(
            self.user_id,
            self.project_id,
            artifact_id=artifact.id,
            run_id=run.id,
            expected_revision=0,
            topic="Copper",
        )
        run_b, artifact_b = self.discovery_inputs(tag="b")
        with self.assertRaises(WorkflowConflict):
            self.repository.save_discovery_atomically(
                self.user_id,
                self.project_id,
                artifact_id=artifact_b.id,
                run_id=run_b.id,
                expected_revision=0,
                status="review",
            )

    def test_unknown_run_is_rejected_by_shared_guard(self) -> None:
        _run, artifact = self.discovery_inputs()
        with self.assertRaises(WorkflowNotFound):
            self.repository.save_discovery_atomically(
                self.user_id,
                self.project_id,
                artifact_id=artifact.id,
                run_id="00000000-0000-0000-0000-000000000000",
                expected_revision=0,
                status="review",
            )


class BearerTokenContractTests(unittest.TestCase):
    """One shared parser now serves the API and the private gateway app."""

    def test_extracts_case_insensitive_bearer_credential(self) -> None:
        from review_writer_api.security import bearer_token

        self.assertEqual("abc", bearer_token("Bearer abc"))
        self.assertEqual("abc", bearer_token("bearer abc"))
        self.assertEqual("abc", bearer_token("BEARER  abc "))

    def test_rejects_missing_or_non_bearer_credentials(self) -> None:
        from review_writer_api.security import bearer_token

        self.assertEqual("", bearer_token(""))
        self.assertEqual("", bearer_token("Basic abc"))
        self.assertEqual("", bearer_token("Bearer"))


class InternalGatewayRouteContractTests(unittest.TestCase):
    """The embedded gateway routes are hidden unless explicitly enabled."""

    def setUp(self) -> None:
        import base64

        from review_writer_api.app import create_app
        from review_writer_api.config import ApiSettings

        self.sessions, self.engine = session_factory()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        settings = ApiSettings(
            review_root=root,
            deployment_mode="hosted",
            database_url=f"sqlite+pysqlite:///{(root / 'api.sqlite3').as_posix()}",
            public_origin="http://testserver",
            credential_encryption_key=base64.urlsafe_b64encode(
                bytes(range(32))
            ).decode("ascii").rstrip("="),
            hosted_workspace_root=root / "users",
            embedded_gateway_routes_enabled=False,
        )
        self.app = create_app(settings, session_factory_override=self.sessions)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def test_disabled_embedded_gateway_routes_return_not_found(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self.app) as client:
            response = client.get(
                "/api/internal/v1/model-responses/any-request",
                headers={"Authorization": "Bearer any-token"},
            )
        self.assertEqual(404, response.status_code)


class StageBoundaryContractTests(unittest.TestCase):
    """A stage must not reach into another stage's module or job types."""

    def _source(self, *parts: str) -> str:
        root = Path(__file__).resolve().parents[2]
        return (root.joinpath("review_writer_api", *parts)).read_text(encoding="utf-8")

    def test_library_stage_never_reaches_into_discovery(self) -> None:
        # Stage 01 acquires papers. Refreshing a Discovery candidate is stage 02's
        # job, so library must not name it, its job types, or its payload fields.
        source = self._source("routers", "library.py")
        self.assertNotIn("discovery", source.casefold())
        self.assertNotIn("acquisition_project_id", source)

    def test_discovery_owns_its_acquisition_chain(self) -> None:
        router = self._source("routers", "discovery.py")
        self.assertIn('"discovery.acquire"', router)
        self.assertNotIn("acquisition_project_id", router)
        handlers = self._source("job_handlers", "discovery_execution.py")
        self.assertIn('register_handler("discovery.acquire"', handlers)
        # The download implementation is reused instead of duplicated.
        self.assertIn('handlers.get("library.download")', handlers)


class CurrentAcquisitionJobContractTests(_SharedProjectCase):
    """Search and download current-job endpoints share one helper."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from review_writer_api.routers.library import build_library_router

        class _FakeJobService:
            def __init__(self, repository):
                self.repository = repository

            def register_handler(self, *args, **kwargs):
                return None

        app = FastAPI()
        app.include_router(
            build_library_router(
                lambda: self.principal,
                library_service=object(),
                library_index_service=object(),
                job_service=_FakeJobService(self.repository),
            )
        )
        self.client = TestClient(app)

    def _seed(self, job_type: str, key: str) -> None:
        self.repository.create_or_get_job(
            self.user_id, None, "library", job_type, key, {}
        )

    def test_both_endpoints_return_their_own_current_job(self) -> None:
        self._seed("library.search", "seed-search")
        self._seed("library.download", "seed-download")
        search = self.client.get("/api/v1/library/search-jobs/current").json()
        download = self.client.get("/api/v1/library/download-jobs/current").json()
        self.assertEqual("library.search", search["job"]["job_type"])
        self.assertEqual("library.download", download["job"]["job_type"])

    def test_current_job_is_scoped_by_job_type(self) -> None:
        self._seed("library.search", "seed-search")
        download = self.client.get("/api/v1/library/download-jobs/current").json()
        self.assertIsNone(download["job"])


class EnvironmentFlagContractTests(unittest.TestCase):
    """Boolean env parsing lives in config and is reused by the migrator."""

    SPELLINGS = (
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        ("off", False),
    )

    def test_config_flag_parses_known_spellings(self) -> None:
        from review_writer_api.config import _environment_flag

        for raw, expected in self.SPELLINGS:
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"RW_TEST_FLAG": raw}):
                    self.assertEqual(expected, _environment_flag("RW_TEST_FLAG", not expected))

    def test_config_flag_falls_back_to_default_when_unset(self) -> None:
        from review_writer_api.config import _environment_flag

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RW_TEST_FLAG", None)
            self.assertTrue(_environment_flag("RW_TEST_FLAG", True))
            self.assertFalse(_environment_flag("RW_TEST_FLAG", False))

    def test_config_flag_rejects_unknown_value(self) -> None:
        from review_writer_api.config import _environment_flag

        with patch.dict(os.environ, {"RW_TEST_FLAG": "maybe"}):
            with self.assertRaises(ValueError):
                _environment_flag("RW_TEST_FLAG", False)

    def test_migration_flag_shares_parsing_and_wraps_the_error(self) -> None:
        from review_writer_api.migration_bootstrap import _flag

        with patch.dict(os.environ, {"RW_TEST_FLAG": "yes"}):
            self.assertTrue(_flag("RW_TEST_FLAG"))
        with patch.dict(os.environ, {"RW_TEST_FLAG": "maybe"}):
            with self.assertRaises(WorkflowMigrationError):
                _flag("RW_TEST_FLAG")


if __name__ == "__main__":
    unittest.main()
