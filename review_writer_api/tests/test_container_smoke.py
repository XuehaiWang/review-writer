from __future__ import annotations

import http.cookiejar
import json
import os
import re
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class ContainerConfigurationTests(unittest.TestCase):
    def test_compose_uses_postgres_migration_gate_and_persistent_migration_paths(self) -> None:
        source = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        for service in ("postgres", "migrate", "api"):
            self.assertRegex(source, rf"(?m)^  {service}:\s*$")
        self.assertEqual(
            2,
            source.count("image: review-writer-api:${REVIEW_WRITER_IMAGE_TAG:-latest}"),
        )
        self.assertNotRegex(source.casefold(), r"(?m)^\s*prefect(?:-|_service|:)")
        self.assertNotIn("PREFECT_", source)
        self.assertIn(
            '"${REVIEW_WRITER_POSTGRES_BIND_ADDRESS:-127.0.0.1}:${REVIEW_WRITER_POSTGRES_BIND_PORT:-5432}:5432"',
            source,
        )
        self.assertIn(
            '"${REVIEW_WRITER_BIND_ADDRESS:-127.0.0.1}:${REVIEW_WRITER_HTTP_PORT:-8770}:8770"',
            source,
        )
        self.assertRegex(
            source,
            r"(?s)migrate:.*?depends_on:\s*\n\s+postgres:\s*\n\s+condition: service_healthy",
        )
        self.assertIn(
            'command: ["python", "-m", "review_writer_api.migration_bootstrap"]',
            source,
        )
        self.assertRegex(
            source,
            r"(?s)api:.*?depends_on:\s*\n\s+migrate:\s*\n\s+condition: service_completed_successfully",
        )
        self.assertIn(
            "${REVIEW_WRITER_MIGRATION_REPORTS_DIR:-./.review-writer/migration-reports}:/app/migration-reports",
            source,
        )
        self.assertIn(
            "${REVIEW_WRITER_MIGRATION_BACKUPS_DIR:-./.review-writer/migration-backups}:/app/migration-backups",
            source,
        )

    def test_api_image_runs_as_non_root_and_has_native_healthcheck(self) -> None:
        source = (ROOT / "Dockerfile.api").read_text(encoding="utf-8")

        self.assertIn("USER reviewwriter", source)
        self.assertIn("/api/v1/health", source)
        self.assertNotIn("prefect", source.casefold())
        self.assertNotIn("workflow.sqlite3", source)

    def test_hosted_example_documents_safe_bind_and_migration_directories(self) -> None:
        source = (ROOT / ".env.hosted.example").read_text(encoding="utf-8")

        self.assertIn("REVIEW_WRITER_BIND_ADDRESS=127.0.0.1", source)
        self.assertIn("REVIEW_WRITER_HTTP_PORT=8770", source)
        self.assertIn("REVIEW_WRITER_POSTGRES_BIND_PORT=5432", source)
        self.assertIn(
            "REVIEW_WRITER_MIGRATION_REPORTS_DIR=./.review-writer/migration-reports",
            source,
        )
        self.assertIn(
            "REVIEW_WRITER_MIGRATION_BACKUPS_DIR=./.review-writer/migration-backups",
            source,
        )
        self.assertIn("REVIEW_WRITER_MIGRATION_ACCEPT_FILE_DRIFT=false", source)

    def test_automatic_migration_bootstrap_is_available(self) -> None:
        from review_writer_api import migration_bootstrap

        self.assertTrue(callable(migration_bootstrap.main))
        self.assertTrue(callable(migration_bootstrap.run_legacy_migration))


class MigrationBootstrapTests(unittest.TestCase):
    def test_fresh_install_writes_report_without_invoking_legacy_import(self) -> None:
        from review_writer_api import migration_bootstrap
        from review_writer_api.workflow_migration import MigrationInventory

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inventory = MigrationInventory(
                workspace_root=str(root / "workspaces"), sources=(), table_counts={}
            )
            with patch.object(
                migration_bootstrap, "inventory_legacy_workflows", return_value=inventory
            ), patch.object(migration_bootstrap, "migrate_legacy_workflows") as migrate:
                result = migration_bootstrap.run_legacy_migration(
                    workspace_root=root / "workspaces",
                    backup_root=root / "backups",
                    report_root=root / "reports",
                    session_factory=object(),
                )

            self.assertEqual("fresh_install", result["status"])
            self.assertTrue(result["ready"])
            migrate.assert_not_called()
            self.assertTrue((root / "reports" / "latest.json").is_file())

    def test_existing_matching_ready_inventory_is_not_imported_again(self) -> None:
        from review_writer_api import migration_bootstrap
        from review_writer_api.workflow_migration import (
            LegacySourceInventory,
            MigrationInventory,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = LegacySourceInventory(
                source_path=str(root / "workflow.sqlite3"),
                review_root=str(root),
                owner_hint="owner",
                is_local=False,
                source_sha256="a" * 64,
                table_counts={"stage_runs": 1},
            )
            inventory = MigrationInventory(
                workspace_root=str(root),
                sources=(source,),
                table_counts={"stage_runs": 1},
            )
            with patch.object(
                migration_bootstrap, "inventory_legacy_workflows", return_value=inventory
            ), patch.object(
                migration_bootstrap, "_already_ready", return_value=True
            ), patch.object(migration_bootstrap, "migrate_legacy_workflows") as migrate:
                result = migration_bootstrap.run_legacy_migration(
                    workspace_root=root,
                    backup_root=root / "backups",
                    report_root=root / "reports",
                    session_factory=object(),
                )

            self.assertEqual("already_migrated", result["status"])
            migrate.assert_not_called()


@unittest.skipUnless(
    os.environ.get("REVIEW_WRITER_RUN_CONTAINER_SMOKE") == "1",
    "Set REVIEW_WRITER_RUN_CONTAINER_SMOKE=1 against a freshly built Compose API.",
)
class LiveContainerSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_url = os.environ.get(
            "REVIEW_WRITER_CONTAINER_BASE_URL", "http://127.0.0.1:8770"
        ).rstrip("/")
        cls.cookies = http.cookiejar.CookieJar()
        cls.client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cls.cookies)
        )
        cls.origin = cls.base_url

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object] | None]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self.origin,
            },
        )
        try:
            with self.client.open(request, timeout=20) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = json.loads(raw) if raw else None
            self.fail(f"{method} {path} returned {exc.code}: {payload}")

    def test_health_auth_project_and_native_workflow(self) -> None:
        health_status, health = self.request("GET", "/api/v1/health")
        self.assertEqual(200, health_status)
        self.assertEqual("ok", health["status"])

        marker = uuid.uuid4().hex
        email = f"container-smoke-{marker}@example.com"
        password = "container-smoke-password-123"
        register_status, registered = self.request(
            "POST",
            "/api/v1/auth/register",
            {"email": email, "password": password, "display_name": "Container smoke"},
        )
        self.assertEqual(201, register_status)
        self.assertEqual(email, registered["email"])

        logout_status, _ = self.request("POST", "/api/v1/auth/logout", {})
        self.assertEqual(204, logout_status)
        login_status, logged_in = self.request(
            "POST", "/api/v1/auth/login", {"email": email, "password": password}
        )
        self.assertEqual(200, login_status)
        self.assertEqual(email, logged_in["email"])

        project_status, project = self.request(
            "POST",
            "/api/v1/projects",
            {
                "slug": f"container-smoke-{marker}",
                "topic": "Container smoke workflow",
                "taxonomy_profile": "chemistry_general",
            },
        )
        self.assertEqual(201, project_status)
        project_id = str(project["project_id"])
        self.assertTrue(re.fullmatch(r"[0-9a-f-]{36}", project_id))

        start_status, job = self.request(
            "POST",
            f"/api/v1/projects/{project_id}/discovery/jobs",
            {
                "topic": "Container smoke workflow",
                "keywords": "container smoke",
                "web_search": False,
            },
        )
        self.assertEqual(202, start_status)
        deadline = time.monotonic() + 30
        while str(job["status"]) not in {"succeeded", "failed", "cancelled", "interrupted"}:
            if time.monotonic() >= deadline:
                self.fail(f"Discovery container smoke timed out: {job}")
            time.sleep(0.1)
            _, job = self.request("GET", f"/api/v1/jobs/{job['id']}")
        self.assertEqual("succeeded", job["status"], job)

        workflow_status, workflow = self.request(
            "GET", f"/api/v1/projects/{project_id}/discovery"
        )
        self.assertEqual(200, workflow_status)
        self.assertIn("results", workflow)

        logout_status, _ = self.request("POST", "/api/v1/auth/logout", {})
        self.assertEqual(204, logout_status)
        second_email = f"container-smoke-second-{marker}@example.com"
        second_status, _ = self.request(
            "POST",
            "/api/v1/auth/register",
            {
                "email": second_email,
                "password": password,
                "display_name": "Container smoke second",
            },
        )
        self.assertEqual(201, second_status)
        projects_status, projects = self.request("GET", "/api/v1/projects")
        self.assertEqual(200, projects_status)
        self.assertNotIn(project_id, {str(item["project_id"]) for item in projects["items"]})
        same_slug_status, isolated_project = self.request(
            "POST",
            "/api/v1/projects",
            {
                "slug": f"container-smoke-{marker}",
                "topic": "Isolated project with the same slug",
                "taxonomy_profile": "chemistry_general",
            },
        )
        self.assertEqual(201, same_slug_status)
        self.assertNotEqual(project_id, str(isolated_project["project_id"]))


if __name__ == "__main__":
    unittest.main()
