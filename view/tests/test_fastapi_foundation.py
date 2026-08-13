import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_writer_api.app import create_app
from review_writer_api.auth import AuthError, PasswordHasher
from review_writer_api.config import ApiSettings
from review_writer_api.credentials import CredentialCipher, ProviderSettingsError
from review_writer_api.database import Base, Project, ProviderCredential, User, UserSession
from review_writer_api.security import Principal, Role


TEST_CREDENTIAL_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


def hosted_settings(root: Path, **updates) -> ApiSettings:
    values = {
        "review_root": root,
        "deployment_mode": "hosted",
        "database_url": "sqlite+pysqlite:///:memory:",
        "public_origin": "http://testserver",
        "credential_encryption_key": TEST_CREDENTIAL_KEY,
        "session_cookie_name": "review_writer_session",
        "session_days": 7,
        "session_cookie_secure": False,
        "expose_api_docs": False,
    }
    values.update(updates)
    return ApiSettings(**values)


def register(client: TestClient, email: str, password: str = "strong-password-123"):
    return client.post(
        "/api/v1/auth/register",
        headers={"Origin": "http://testserver"},
        json={"email": email, "password": password, "display_name": email.split("@", 1)[0]},
    )


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        project = self.root / "review-projects" / "copper-review"
        (project / "00_discovery").mkdir(parents=True)
        (project / "01_matrix_outline").mkdir(parents=True)
        (project / "project_config.json").write_text(
            json.dumps(
                {
                    "project_id": "copper-review",
                    "topic": "Mechanochemical copper activation",
                    "taxonomy_profile": "chemistry_general",
                }
            ),
            encoding="utf-8",
        )
        (project / "00_discovery" / "combined_results_by_keyword.json").write_text(
            json.dumps({"topic": "fallback topic", "results": []}), encoding="utf-8"
        )
        (project / "00_discovery" / "human_check_state.json").write_text(
            json.dumps({"status": "approved"}), encoding="utf-8"
        )
        (project / "01_matrix_outline" / "literature_matrix.json").write_text(
            json.dumps({"rows": []}), encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_local_portal_catalog_and_creation(self):
        with TestClient(create_app(ApiSettings(review_root=self.root))) as client:
            health = client.get("/api/v1/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.headers["X-Request-ID"])
            identity = client.get("/api/v1/me").json()
            self.assertEqual(identity["display_name"], "Local owner")
            self.assertFalse(client.get("/api/v1/auth/config").json()["enabled"])

            projects = client.get("/api/v1/projects").json()
            self.assertEqual(projects["count"], 1)
            self.assertEqual(projects["items"][0]["completed_stages"], ["discovery", "matrix"])
            created = client.post(
                "/api/v1/projects", json={"slug": "new-review", "topic": "New topic"}
            )
            self.assertEqual(created.status_code, 201)
            self.assertTrue(
                (self.root / "review-projects" / "new-review" / "project_config.json").is_file()
            )

            portal = client.get("/")
            self.assertIn("script-src 'self'", portal.headers["Content-Security-Policy"])
            self.assertEqual(portal.headers["X-Frame-Options"], "DENY")
            self.assertIn("HttpOnly Cookie", portal.text)
            script = client.get("/assets/app/app.js").text
            self.assertIn("/api/v1/auth/${mode}", script)
            self.assertNotIn("code_challenge", script)

            pdf_path = self.root / "review-library" / "sources" / "P001.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(b"%PDF-1.4\n" + (b"0" * 512) + b"\n%%EOF\n")
            preview = client.get("/file?path=review-library/sources/P001.pdf")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.headers["Content-Type"], "application/pdf")
            self.assertEqual(preview.headers["X-Frame-Options"], "SAMEORIGIN")
            self.assertEqual(
                preview.headers["Content-Security-Policy"],
                "frame-ancestors 'self'",
            )

            ketcher = client.get("/assets/ketcher/standalone/index.html")
            self.assertEqual(ketcher.status_code, 200)
            self.assertIn("Ketcher v3.17.0", ketcher.text)
            self.assertEqual(ketcher.headers["X-Frame-Options"], "SAMEORIGIN")
            self.assertEqual(
                ketcher.headers["Content-Security-Policy"],
                "frame-ancestors 'self'",
            )
            dashboard_asset = client.get("/assets/dashboard/review-ui.js")
            self.assertEqual(dashboard_asset.status_code, 200)
            self.assertEqual(dashboard_asset.headers["X-Frame-Options"], "DENY")
            self.assertIn("no-store", dashboard_asset.headers["Cache-Control"])
            dashboard_page = client.get("/sections")
            self.assertEqual(dashboard_page.status_code, 200)
            self.assertIn("no-store", dashboard_page.headers["Cache-Control"])
            self.assertEqual(client.get("/assets/%2e%2e/README.md").status_code, 404)

    def test_permission_and_local_user_scope(self):
        no_role = Principal(user_id="user", roles=frozenset())
        with TestClient(
            create_app(ApiSettings(review_root=self.root), principal_provider=lambda: no_role)
        ) as client:
            self.assertEqual(client.get("/api/v1/projects").status_code, 403)

        another = Principal(user_id="another-user", roles=frozenset({Role.USER}))
        with TestClient(
            create_app(ApiSettings(review_root=self.root), principal_provider=lambda: another)
        ) as client:
            self.assertEqual(client.get("/api/v1/projects").json()["items"], [])


class PasswordAndConfigurationTests(unittest.TestCase):
    def test_password_hash_is_salted_and_not_reversible(self):
        hasher = PasswordHasher()
        first = hasher.hash("strong-password-123")
        second = hasher.hash("strong-password-123")
        self.assertNotEqual(first, second)
        self.assertNotIn("strong-password", first)
        self.assertTrue(hasher.verify("strong-password-123", first))
        self.assertFalse(hasher.verify("wrong-password", first))
        with self.assertRaises(AuthError):
            hasher.hash("short")

    def test_hosted_environment_uses_postgres_configuration_without_identity_provider(self):
        environment = {
            "REVIEW_WRITER_DEPLOYMENT_MODE": "hosted",
            "REVIEW_WRITER_POSTGRES_HOST": "db",
            "REVIEW_WRITER_POSTGRES_USER": "user",
            "REVIEW_WRITER_POSTGRES_PASSWORD": "p@ss/word",
            "REVIEW_WRITER_POSTGRES_DB": "review_writer",
            "REVIEW_WRITER_PUBLIC_ORIGIN": "https://review.example.com",
            "REVIEW_WRITER_CREDENTIAL_ENCRYPTION_KEY": TEST_CREDENTIAL_KEY,
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = ApiSettings.from_env(ROOT)
        self.assertTrue(settings.session_cookie_secure)
        self.assertFalse(settings.expose_api_docs)
        self.assertEqual(settings.public_origin, "https://review.example.com")
        self.assertIn("p%40ss%2Fword", settings.database_url)
        self.assertFalse(any("OIDC" in key or "KEYCLOAK" in key for key in environment))

        environment["REVIEW_WRITER_PUBLIC_ORIGIN"] = "http://review.example.com"
        with patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(
            ValueError, "Secure session cookies"
        ):
            ApiSettings.from_env(ROOT)

    def test_provider_settings_import_names_share_runtime_credentials(self):
        import importlib

        legacy_module = importlib.import_module("provider_settings")
        package_module = importlib.import_module("view.provider_settings")
        self.assertIs(legacy_module, package_module)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_module.register_runtime_provider_environment(
                root,
                {"MINERU_API_TOKEN": "mineru-runtime-token"},
                isolated=True,
            )
            environment = legacy_module.provider_subprocess_environment(root)
            self.assertEqual(environment["MINERU_API_TOKEN"], "mineru-runtime-token")


class DatabaseSchemaTests(unittest.TestCase):
    def test_schema_is_direct_user_owned_and_contains_database_sessions(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({"users", "user_sessions", "projects", "provider_credentials"}.issubset(tables))
        self.assertNotIn("organizations", tables)
        self.assertNotIn("memberships", tables)
        engine.dispose()


class HostedAuthApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.settings = hosted_settings(self.root)
        self.app = create_app(self.settings, session_factory_override=self.session_factory)

    def tearDown(self):
        self.engine.dispose()
        self.temporary.cleanup()

    def test_register_cookie_me_logout_and_login(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/api/v1/me").status_code, 401)
            registered = register(client, "chemist@example.com")
            self.assertEqual(registered.status_code, 201)
            self.assertEqual(registered.json()["email"], "chemist@example.com")
            cookie = registered.headers.get("set-cookie", "")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertNotIn("Secure", cookie)
            self.assertEqual(client.get("/api/v1/me").status_code, 200)

            duplicate = register(client, "CHEMIST@example.com")
            self.assertEqual(duplicate.status_code, 409)
            logged_out = client.post(
                "/api/v1/auth/logout", headers={"Origin": "http://testserver"}
            )
            self.assertEqual(logged_out.status_code, 204)
            self.assertEqual(client.get("/api/v1/me").status_code, 401)

            wrong = client.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://testserver"},
                json={"email": "chemist@example.com", "password": "wrong"},
            )
            self.assertEqual(wrong.status_code, 401)
            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://testserver"},
                json={"email": "chemist@example.com", "password": "strong-password-123"},
            )
            self.assertEqual(logged_in.status_code, 200)

        database = self.session_factory()
        try:
            user = database.query(User).one()
            self.assertNotIn("strong-password", user.password_hash)
            self.assertEqual(database.query(UserSession).count(), 2)
        finally:
            database.close()

    def test_projects_are_isolated_and_cross_origin_writes_are_rejected(self):
        with TestClient(self.app) as first, TestClient(self.app) as second:
            self.assertEqual(register(first, "first@example.com").status_code, 201)
            self.assertEqual(register(second, "second@example.com").status_code, 201)
            created_first = first.post(
                "/api/v1/projects",
                headers={"Origin": "http://testserver"},
                json={"slug": "same-project", "topic": "First"},
            )
            self.assertEqual(created_first.status_code, 201)
            self.assertEqual(second.get("/api/v1/projects").json()["count"], 0)
            self.assertEqual(
                second.get("/api/v1/projects/same-project").status_code, 404
            )
            created_second = second.post(
                "/api/v1/projects",
                headers={"Origin": "http://testserver"},
                json={"slug": "same-project", "topic": "Second"},
            )
            self.assertEqual(created_second.status_code, 201)
            self.assertNotEqual(
                created_first.json()["project_id"], created_second.json()["project_id"]
            )
            blocked = first.post(
                "/api/v1/projects",
                headers={"Origin": "https://attacker.example"},
                json={"slug": "blocked", "topic": "Blocked"},
            )
            self.assertEqual(blocked.status_code, 403)

    def test_native_project_catalog_uses_database_state_while_legacy_route_can_backfill(self):
        from view.serve_review_dashboard import workflow_store

        origin = {"Origin": "http://testserver"}
        with TestClient(self.app) as client:
            identity = register(client, "catalog-owner@example.com").json()
            created = client.post(
                "/api/v1/projects",
                headers=origin,
                json={"slug": "catalog-project", "topic": "Database-owned topic"},
            )
            self.assertEqual(created.status_code, 201)

            user_root = self.app.state.hosted_workspace_manager.user_root(identity["user_id"])
            project = user_root / "review-projects" / "catalog-project"
            project.mkdir(parents=True)
            (project / "project_config.json").write_text(
                json.dumps(
                    {
                        "project_id": "catalog-project",
                        "topic": "Stale filesystem topic",
                        "taxonomy_profile": "chemistry_general",
                    }
                ),
                encoding="utf-8",
            )
            store = workflow_store(user_root)
            store.set_stage_state("catalog-project", "discovery", "approved")
            store.set_stage_state("catalog-project", "matrix", "needs_human_review")

            portal_project = client.get("/api/v1/projects").json()["items"][0]
            self.assertEqual(portal_project["topic"], "Database-owned topic")
            self.assertEqual(portal_project["current_stage"], "discovery")
            self.assertEqual(portal_project["completed_stages"], [])

            legacy_project = client.get("/api/projects").json()[0]
            self.assertEqual(legacy_project["topic"], "Database-owned topic")
            self.assertEqual(legacy_project["current_stage"], "matrix")

        database = self.session_factory()
        try:
            project_row = database.query(Project).filter_by(slug="catalog-project").one()
            self.assertEqual(project_row.current_stage, "matrix")
            self.assertEqual(project_row.stage_states["matrix"]["status"], "needs_human_review")
        finally:
            database.close()

    def test_legacy_sync_failure_does_not_break_native_project_catalog(self):
        origin = {"Origin": "http://testserver"}
        with TestClient(self.app) as client:
            identity = register(client, "sync-error@example.com").json()
            created = client.post(
                "/api/v1/projects",
                headers=origin,
                json={"slug": "sync-error-project", "topic": "Sync error"},
            )
            self.assertEqual(created.status_code, 201)
            project = (
                self.app.state.hosted_workspace_manager.user_root(identity["user_id"])
                / "review-projects"
                / "sync-error-project"
            )
            project.mkdir(parents=True)

            with patch(
                "view.serve_review_dashboard.reconcile_project_semantic_states",
                side_effect=OSError("intentional sync failure"),
            ):
                native_response = client.get("/api/v1/projects")
                legacy_response = client.get("/api/projects")

            self.assertEqual(native_response.status_code, 200)
            self.assertEqual(native_response.json()["items"][0]["current_stage"], "discovery")
            self.assertEqual(legacy_response.status_code, 500)
            self.assertIn("PostgreSQL was not updated", legacy_response.json()["detail"])

    def test_topic_restart_updates_only_the_owned_project_after_discovery_succeeds(self):
        origin = {"Origin": "http://testserver"}
        with TestClient(self.app) as first, TestClient(self.app) as second:
            first_identity = register(first, "restart-first@example.com").json()
            second_identity = register(second, "restart-second@example.com").json()
            for client, topic in ((first, "First old topic"), (second, "Second old topic")):
                created = client.post(
                    "/api/v1/projects",
                    headers=origin,
                    json={"slug": "shared-restart", "topic": topic},
                )
                self.assertEqual(created.status_code, 201)

            manager = self.app.state.hosted_workspace_manager
            for identity, topic in (
                (first_identity, "First old topic"),
                (second_identity, "Second old topic"),
            ):
                discovery = (
                    manager.user_root(identity["user_id"])
                    / "review-projects"
                    / "shared-restart"
                    / "00_discovery"
                )
                discovery.mkdir(parents=True)
                (discovery / "combined_results_by_keyword.json").write_text(
                    json.dumps({"topic": topic, "selection_mode": "explicit", "results": []}),
                    encoding="utf-8",
                )

            with patch(
                "view.serve_review_dashboard.start_discovery",
                return_value={"ok": False, "project_id": "shared-restart", "error": "Discovery failed."},
            ):
                failed = first.post(
                    "/api/discovery",
                    headers=origin,
                    json={
                        "project_id": "shared-restart",
                        "topic": "Failed replacement topic",
                        "restart_existing": True,
                    },
                )
            self.assertEqual(failed.status_code, 502)
            self.assertEqual(
                first.get("/api/v1/projects").json()["items"][0]["topic"],
                "First old topic",
            )

            with patch(
                "view.serve_review_dashboard.start_discovery",
                return_value={
                    "ok": True,
                    "project_id": "shared-restart",
                    "output": "complete",
                    "restarted": True,
                },
            ):
                changed = first.post(
                    "/api/discovery",
                    headers=origin,
                    json={
                        "project_id": "shared-restart",
                        "topic": "First replacement topic",
                        "restart_existing": True,
                    },
                )
            self.assertEqual(changed.status_code, 201)
            self.assertEqual(
                first.get("/api/v1/projects").json()["items"][0]["topic"],
                "First replacement topic",
            )
            self.assertEqual(
                second.get("/api/v1/projects").json()["items"][0]["topic"],
                "Second old topic",
            )

    def test_same_slug_batch_redraw_state_is_scoped_by_user_root(self):
        from view import serve_review_dashboard as dashboard

        first_root = self.root / "first-user"
        second_root = self.root / "second-user"
        project_slug = "same-project"
        first_key = dashboard._batch_redraw_key(first_root, project_slug)
        second_key = dashboard._batch_redraw_key(second_root, project_slug)
        with dashboard._BATCH_REDRAW_LOCK:
            dashboard._BATCH_REDRAW_JOBS[first_key] = {
                "status": "running",
                "owner_marker": "first",
                "errors": [],
            }
            dashboard._BATCH_REDRAW_JOBS[second_key] = {
                "status": "running",
                "owner_marker": "second",
                "errors": [],
            }
        try:
            self.assertEqual(
                dashboard.batch_figure_redraw_status(project_slug, first_root)["owner_marker"],
                "first",
            )
            self.assertEqual(
                dashboard.batch_figure_redraw_status(project_slug, second_root)["owner_marker"],
                "second",
            )
            self.assertEqual(
                dashboard.batch_figure_redraw_status(project_slug)["status"],
                "idle",
            )
        finally:
            with dashboard._BATCH_REDRAW_LOCK:
                dashboard._BATCH_REDRAW_JOBS.pop(first_key, None)
                dashboard._BATCH_REDRAW_JOBS.pop(second_key, None)

    def test_provider_keys_are_encrypted_and_user_isolated(self):
        with TestClient(self.app) as first, TestClient(self.app) as second:
            register(first, "first@example.com")
            register(second, "second@example.com")
            saved = first.put(
                "/api/v1/provider-settings/text",
                headers={"Origin": "http://testserver"},
                json={
                    "base_url": "https://models.example/v1",
                    "model_name": "chemistry-model",
                    "wire_api": "responses",
                    "api_key": "first-user-secret-key",
                },
            )
            self.assertEqual(saved.status_code, 200)
            self.assertNotIn("api_key", saved.json())
            self.assertEqual(second.get("/api/v1/provider-settings").json()["items"], [])

        database = self.session_factory()
        try:
            row = database.query(ProviderCredential).one()
            self.assertNotIn(b"first-user-secret-key", row.encrypted_secret)
            cipher = CredentialCipher(TEST_CREDENTIAL_KEY)
            self.assertEqual(
                cipher.decrypt(str(row.user_id), "text", row.encrypted_secret),
                "first-user-secret-key",
            )
            with self.assertRaises(ProviderSettingsError):
                cipher.decrypt(str(Project().id or "00000000-0000-0000-0000-000000000001"), "text", row.encrypted_secret)
        finally:
            database.close()

    def test_hosted_mineru_token_reaches_workflow_pdf_parser_environment(self):
        captured: dict[str, object] = {}

        class FailedParse:
            returncode = 1
            stdout = ""
            stderr = "intentional parser stop"

        def fake_run(_command, **kwargs):
            captured["environment"] = kwargs.get("env")
            return FailedParse()

        with TestClient(self.app) as client:
            register(client, "mineru-worker@example.com")
            saved = client.put(
                "/api/v1/provider-settings/mineru",
                headers={"Origin": "http://testserver"},
                json={
                    "base_url": "",
                    "model_name": "",
                    "wire_api": "",
                    "api_key": "mineru-hosted-secret",
                },
            )
            self.assertEqual(saved.status_code, 200)
            with patch("local_pdf_ingestion.subprocess.run", side_effect=fake_run):
                uploaded = client.post(
                    "/api/library/upload-pdf?filename=token-check.pdf",
                    headers={
                        "Origin": "http://testserver",
                        "Content-Type": "application/pdf",
                    },
                    content=(
                        b"%PDF-1.4\n% token bridge test\n"
                        + (b"0" * 512)
                        + b"\n%%EOF\n"
                    ),
                )

        self.assertEqual(uploaded.status_code, 502)
        environment = captured.get("environment") or {}
        self.assertEqual(environment["MINERU_API_TOKEN"], "mineru-hosted-secret")

    def test_seven_stage_compatibility_routes_use_user_scoped_workspaces(self):
        from view.provider_settings import provider_subprocess_environment

        origin = {"Origin": "http://testserver"}
        with TestClient(self.app) as first, TestClient(self.app) as second:
            first_identity = register(first, "first-stage@example.com").json()
            second_identity = register(second, "second-stage@example.com").json()
            for client, topic in ((first, "First private topic"), (second, "Second private topic")):
                created = client.post(
                    "/api/v1/projects",
                    headers=origin,
                    json={"slug": "shared-slug", "topic": topic},
                )
                self.assertEqual(created.status_code, 201)

            manager = self.app.state.hosted_workspace_manager
            first_root = manager.user_root(first_identity["user_id"])
            second_root = manager.user_root(second_identity["user_id"])
            for root, topic in (
                (first_root, "First private topic"),
                (second_root, "Second private topic"),
            ):
                discovery = root / "review-projects" / "shared-slug" / "00_discovery"
                discovery.mkdir(parents=True)
                (discovery / "combined_results_by_keyword.json").write_text(
                    json.dumps({"topic": topic, "selection_mode": "explicit", "results": []}),
                    encoding="utf-8",
                )
                (discovery / "selected_discovery_results.json").write_text(
                    json.dumps({"human_confirmed": False, "local_papers": []}),
                    encoding="utf-8",
                )

            for stage_page in (
                "/library",
                "/discovery",
                "/planning?tab=matrix",
                "/planning?tab=blueprint",
                "/sections",
                "/images?tab=review",
                "/images?tab=redraw",
                "/draft",
                "/final",
            ):
                self.assertEqual(first.get(stage_page).status_code, 200, stage_page)
            for legacy_page, expected_path in (
                ("/matrix?project=shared-slug", "/planning?project=shared-slug&tab=matrix"),
                ("/blueprint?project=shared-slug", "/planning?project=shared-slug&tab=blueprint"),
                ("/figure-review?project=shared-slug", "/images?project=shared-slug&tab=review"),
                ("/figures?project=shared-slug", "/images?project=shared-slug&tab=redraw"),
            ):
                response = first.get(legacy_page, follow_redirects=False)
                self.assertEqual(response.status_code, 307, legacy_page)
                self.assertEqual(response.headers["location"], expected_path)
            self.assertIn("shared-slug", first.get("/api/projects").text)
            self.assertEqual(first.get("/api/discovery/shared-slug").json()["topic"], "First private topic")
            self.assertEqual(second.get("/api/discovery/shared-slug").json()["topic"], "Second private topic")

            compatibility_projects = first.get("/api/projects")
            self.assertEqual(compatibility_projects.headers["Deprecation"], "true")
            self.assertIn('rel="successor-version"', compatibility_projects.headers["Link"])
            removed_settings = first.get("/api/settings")
            self.assertEqual(removed_settings.status_code, 410)
            self.assertEqual(
                removed_settings.json()["detail"],
                "请在用户门户的个人 API 设置中管理模型密钥。",
            )

            changed = first.put(
                "/api/discovery/shared-slug",
                headers=origin,
                json={"topic": "First changed topic", "selection_mode": "explicit", "results": []},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(first.get("/api/discovery/shared-slug").json()["topic"], "First changed topic")
            self.assertEqual(second.get("/api/discovery/shared-slug").json()["topic"], "Second private topic")
            self.assertEqual(
                first.get("/api/v1/projects").json()["items"][0]["topic"],
                "First changed topic",
            )
            first_config = json.loads(
                (first_root / "review-projects" / "shared-slug" / "project_config.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(first_config["topic"], "First changed topic")
            empty_confirmation = first.put(
                "/api/discovery/shared-slug?confirm=1",
                headers=origin,
                json={"topic": "First changed topic", "selection_mode": "explicit", "results": []},
            )
            self.assertEqual(empty_confirmation.status_code, 409)
            self.assertFalse(empty_confirmation.json()["confirmed"])
            self.assertIn("at least one", empty_confirmation.json()["error"])

            second.post(
                "/api/v1/projects",
                headers=origin,
                json={"slug": "second-only", "topic": "Second only"},
            )
            self.assertEqual(first.get("/api/project/second-only/matrix").status_code, 404)
            blocked_settings = first.put("/api/settings", headers=origin, json={})
            self.assertEqual(blocked_settings.status_code, 410)
            self.assertFalse((first_root / ".review-writer" / "provider-settings.json").exists())

            for client, key in ((first, "first-runtime-secret"), (second, "second-runtime-secret")):
                saved = client.put(
                    "/api/v1/provider-settings/text",
                    headers=origin,
                    json={
                        "base_url": "https://models.example/v1",
                        "model_name": "chemistry-model",
                        "wire_api": "responses",
                        "api_key": key,
                    },
                )
                self.assertEqual(saved.status_code, 200)
                refreshed = client.put(
                    "/api/discovery/shared-slug",
                    headers=origin,
                    json={"topic": "Private", "selection_mode": "explicit", "results": []},
                )
                self.assertEqual(refreshed.status_code, 200)

            self.assertEqual(
                provider_subprocess_environment(first_root)["REVIEW_WRITING_API_KEY"],
                "first-runtime-secret",
            )
            self.assertEqual(
                provider_subprocess_environment(second_root)["REVIEW_WRITING_API_KEY"],
                "second-runtime-secret",
            )

            deleted = first.delete("/api/projects/shared-slug", headers=origin)
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(first.get("/api/v1/projects").json()["count"], 0)
            self.assertEqual(second.get("/api/v1/projects").json()["count"], 2)
            self.assertFalse((first_root / "review-projects" / "shared-slug").exists())
            self.assertTrue((second_root / "review-projects" / "shared-slug").exists())

        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.get("/library").status_code, 401)


if __name__ == "__main__":
    unittest.main()
