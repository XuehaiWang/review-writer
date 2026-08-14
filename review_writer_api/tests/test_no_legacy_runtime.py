from __future__ import annotations

import base64
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base
from review_writer_api.security import Principal, Role


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "view" / "assets" / "dashboard"
TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
REMOVED_RUNTIME = (
    ROOT / "review_writer_api" / "workflow_compat.py",
    ROOT / "review_writer_api" / "dashboard_executor.py",
    ROOT / "view" / "serve_review_dashboard.py",
    ROOT / "view" / "prefect_runtime.py",
    ROOT / "view" / "prefect_flows.py",
    ROOT / "view" / "provider_settings.py",
)
LEGACY_IMPORTS = (
    "view.serve_review_dashboard",
    "view.workflow_store",
    "view.prefect_runtime",
    "view.prefect_flows",
    "view.provider_settings",
    "review_writer_api.workflow_compat",
    "review_writer_api.dashboard_executor",
)


class NoLegacyRuntimeTests(unittest.TestCase):
    def test_normal_app_import_does_not_load_legacy_runtime_or_create_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            script = (
                "import pathlib,sys; "
                f"root=pathlib.Path({raw!r}); "
                "from review_writer_api.app import create_app; "
                "from review_writer_api.config import ApiSettings; "
                "create_app(ApiSettings(review_root=root)); "
                f"blocked={LEGACY_IMPORTS!r}; "
                "assert not [name for name in blocked if name in sys.modules], "
                "[name for name in blocked if name in sys.modules]; "
                "assert not list(root.rglob('workflow.sqlite3')); "
                "assert not list(root.rglob('prefect*'))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)

    def test_compatibility_and_prefect_runtime_modules_are_removed(self) -> None:
        self.assertEqual([], [str(path) for path in REMOVED_RUNTIME if path.exists()])
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").casefold()
        self.assertNotIn("prefect", requirements)

    def test_ci_migrates_and_tests_against_postgresql(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "api-foundation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("postgres:17", workflow)
        self.assertIn("REVIEW_WRITER_RUN_POSTGRES_TESTS: \"1\"", workflow)
        self.assertNotIn("sqlite+pysqlite", workflow)

    def test_dashboard_workflow_requests_use_only_versioned_api(self) -> None:
        offenders: list[str] = []
        for path in sorted(DASHBOARD.glob("*")):
            if path.suffix not in {".html", ".js"}:
                continue
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"(?:fetch\s*\(|endpoint\s*:\s*)[`'\"](/api/[^`'\"${}]*)", source):
                route = match.group(1)
                if not route.startswith("/api/v1/"):
                    offenders.append(f"{path.name}: {route}")
            if "/api/project/" in source or "/file?" in source:
                offenders.append(f"{path.name}: legacy workflow/file route")
        self.assertEqual([], offenders)

    def test_native_dashboard_is_mounted_without_compatibility_gateway(self) -> None:
        source = (ROOT / "review_writer_api" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("WorkflowCompatibilityGateway", source)
        self.assertNotIn("workflow_gateway", source)
        self.assertIn("dashboard_page_paths", source)
        self.assertRegex(source, r'app\.mount\(\s*"/assets"')

    def test_retired_local_mode_does_not_mount_nonfunctional_workflow_pages(self) -> None:
        with tempfile.TemporaryDirectory() as raw, TestClient(
            create_app(ApiSettings(review_root=Path(raw)))
        ) as client:
            statuses = {
                path: client.get(path, follow_redirects=False).status_code
                for path in (
                    "/library",
                    "/discovery",
                    "/planning",
                    "/sections",
                    "/images",
                    "/draft",
                    "/final",
                )
            }
        self.assertEqual({path: 404 for path in statuses}, statuses)

    def test_hosted_mode_mounts_all_seven_pages_and_native_stage_routes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            principal = Principal(
                "00000000-0000-0000-0000-000000000001",
                frozenset({Role.USER}),
                "smoke@example.com",
            )
            app = create_app(
                ApiSettings(
                    review_root=Path(raw),
                    deployment_mode="hosted",
                    database_url="sqlite+pysqlite:///:memory:",
                    public_origin="http://testserver",
                    credential_encryption_key=TEST_KEY,
                    hosted_workspace_root=Path(raw) / "users",
                ),
                principal_provider=lambda: principal,
                session_factory_override=sessions,
            )
            with TestClient(app) as client:
                statuses = {
                    path: client.get(path, follow_redirects=False).status_code
                    for path in (
                        "/library",
                        "/discovery",
                        "/planning",
                        "/sections",
                        "/images",
                        "/draft",
                        "/final",
                    )
                }
            # FastAPI 0.123+ retains included APIRouters as lazy route groups;
            # the generated OpenAPI document is the public flattened contract.
            route_paths = set(app.openapi()["paths"])
            engine.dispose()
        self.assertEqual({path: 200 for path in statuses}, statuses)
        for path in (
            "/api/v1/library/papers",
            "/api/v1/projects/{project_id}/discovery",
            "/api/v1/projects/{project_id}/planning",
            "/api/v1/projects/{project_id}/sections",
            "/api/v1/projects/{project_id}/figures",
            "/api/v1/projects/{project_id}/draft",
            "/api/v1/projects/{project_id}/final",
        ):
            with self.subTest(path=path):
                self.assertIn(path, route_paths)


if __name__ == "__main__":
    unittest.main()
