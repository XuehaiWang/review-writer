"""Catalog persistence, enqueue snapshots and exact provider routing contracts."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from review_writer_api.tests import test_server_provider_admin as fixtures
from review_writer_api.model_catalog import read_catalog, save_catalog, resolve_model_tier, SNAPSHOT_KEY
from review_writer_api.model_gateway import ModelGatewayService
from review_writer_api.repositories import HostedProjectRepository
from review_writer_api.workflow_repository import WorkflowRepository
from review_writer_api.security import AuthorizationError
from review_writer_api.errors import WorkflowValidationError


class ModelCatalogTests(unittest.TestCase):
    setUp = fixtures.ServerProviderAdminTests.setUp
    tearDown = fixtures.ServerProviderAdminTests.tearDown

    def configure(self):
        data = read_catalog(self.sessions)
        model = dict(data["items"][0], id="custom", model="Vendor/Exact-Model-V2",
                     label_zh="自定义模型", label_en="Custom model", wire_api="chat-completions",
                     input_usd_per_million="1.25", output_usd_per_million="3.75")
        data["items"].append(model)
        data["default_tier"] = "custom"
        return save_catalog(self.sessions, self.admin, data)

    def project(self):
        return HostedProjectRepository(self.sessions).create_for_user(self.user.user_id,
            slug="test", topic="test", taxonomy_profile="general_academic", model_tier=None)

    def enqueue(self, project, key="first", payload=None):
        return WorkflowRepository(self.sessions).create_or_get_job(self.user.user_id,
            project.project_id, "project", "sections.generate", key, payload or {})

    def test_catalog_is_persistent_and_not_a_process_global(self):
        self.configure()
        self.assertEqual("Vendor/Exact-Model-V2", resolve_model_tier(None, self.sessions).model)
        self.assertEqual("gpt-5.6-terra", resolve_model_tier(None).model)
        self.assertEqual("custom", self.project().model_tier)

    def test_only_admin_can_save(self):
        with self.assertRaises(AuthorizationError):
            save_catalog(self.sessions, self.user, read_catalog(self.sessions))

    def test_public_api_catalog_and_admin_permissions(self):
        from fastapi.testclient import TestClient
        from review_writer_api.app import create_app
        for principal, expected in ((self.user, 403), (self.admin, 200)):
            app = create_app(self.settings, session_factory_override=self.sessions, principal_provider=lambda: principal)
            with TestClient(app) as client:
                response = client.get("/api/v1/model-catalog")
                self.assertEqual(200, response.status_code)
                data = response.json()
                data["items"][0]["label_zh"] = "Updated"
                saved = client.put("/api/v1/admin/model-catalog", json=data)
                self.assertEqual(expected, saved.status_code, saved.text)

    def test_no_deletion_or_disabled_default_or_stale_overwrite(self):
        original = self.configure()
        for mode in ("delete", "disabled", "stale", "negative", "nan", "duplicate"):
            data = read_catalog(self.sessions)
            if mode == "delete": data["items"].pop(0)
            if mode == "disabled": data["items"][-1]["enabled"] = False
            if mode == "stale": data["revision"] = 0
            if mode == "negative": data["items"][-1]["output_usd_per_million"] = "-1"
            if mode == "nan": data["items"][-1]["output_usd_per_million"] = "NaN"
            if mode == "duplicate": data["items"].append(data["items"][0])
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                save_catalog(self.sessions, self.admin, data)
        self.assertEqual(original, read_catalog(self.sessions))

    def test_enqueued_snapshot_survives_catalog_and_project_changes(self):
        data = self.configure()
        project = self.project()
        job = self.enqueue(project, payload={SNAPSHOT_KEY: {"model": "forged"}})
        self.assertEqual("Vendor/Exact-Model-V2", job.payload[SNAPSHOT_KEY]["model"])
        data["items"][-1].update(model="Different", input_usd_per_million="9", enabled=False)
        data["default_tier"] = "terra"
        save_catalog(self.sessions, self.admin, data)
        # Idempotent replay must return the original snapshot even after disabling.
        self.assertEqual(job.id, self.enqueue(project).id)
        HostedProjectRepository(self.sessions).update_model_tier_for_user(self.user.user_id, project.project_id, model_tier="luna")
        gateway = ModelGatewayService(self.sessions, self.settings)
        try:
            token = gateway.issue_task_token(job_id=job.id, user_id=self.user.user_id,
                project_id=project.project_id, job_type="sections.generate")
            model = gateway._claims_model(gateway.verify_task_token(token))
            self.assertEqual("Vendor/Exact-Model-V2", model.model)
            self.assertEqual("1.25", str(model.input_usd_per_million))
            self.assertEqual("chat-completions", model.wire_api)
        finally:
            asyncio.run(gateway.close())

    def test_disabled_selection_rejects_new_jobs_but_not_export(self):
        data = self.configure()
        project = self.project()
        data["items"][-1]["enabled"] = False
        data["default_tier"] = "terra"
        save_catalog(self.sessions, self.admin, data)
        with self.assertRaises(WorkflowValidationError):
            self.enqueue(project)
        job = WorkflowRepository(self.sessions).create_or_get_job(self.user.user_id,
            project.project_id, "project", "final.export-docx", "export", {})
        self.assertNotIn(SNAPSHOT_KEY, job.payload)

    def test_gateway_uses_exact_model_and_protocol_override(self):
        self.configure()
        model = resolve_model_tier("custom", self.sessions)
        gateway = ModelGatewayService(self.sessions, self.settings)
        response = type("Reply", (), {"status_code": 200, "json": lambda _: {"choices": [{"message": {"content": '{"ok":true}'}}]}})()
        try:
            with patch.object(gateway._provider_client, "post", new_callable=AsyncMock, return_value=response) as post:
                asyncio.run(gateway._provider_call(tier=model, prompt="test", idempotency_key="test"))
                self.assertTrue(post.call_args.args[0].endswith("/chat/completions"))
                self.assertEqual("Vendor/Exact-Model-V2", post.call_args.kwargs["json"]["model"])
        finally:
            asyncio.run(gateway.close())

    def test_model_check_rejects_http_200_without_expected_json(self):
        self.configure()
        client = AsyncMock()
        client.__aenter__.return_value = client
        for content, expected in (("plain text", False), ('{"ok":true}', True)):
            response = type("Reply", (), {"status_code": 200, "json": lambda _: {"choices": [{"message": {"content": content}}]}})()
            client.post.return_value = response
            with patch("review_writer_api.server_providers.httpx.AsyncClient", return_value=client):
                result = asyncio.run(self.service.test_connection(self.admin, "text", model_id="custom"))
            self.assertEqual(expected, result.ok)
