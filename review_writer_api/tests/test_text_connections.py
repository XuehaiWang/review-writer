"""Multiple provider keys, immutable job routing and admin-only connection metadata."""
import asyncio
import uuid
import unittest
from unittest.mock import AsyncMock, patch

from review_writer_api.tests import test_server_provider_admin as fixtures
from review_writer_api.database import database_session
from review_writer_api.workflow_models import WorkflowJob, WorkflowSystemState
from review_writer_api.text_connections import CONNECTIONS_KEY, list_connections, save_connection, runtime_for_connection
from review_writer_api.credentials import ProviderSettingsError
from review_writer_api.model_catalog import read_catalog, save_catalog, SNAPSHOT_KEY, model_from_dict, snapshot_for_job
from review_writer_api.model_gateway import ModelGatewayService
from review_writer_api.repositories import HostedProjectRepository
from review_writer_api.workflow_repository import WorkflowRepository
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.security import AuthorizationError


class TextConnectionTests(unittest.TestCase):
    setUp = fixtures.ServerProviderAdminTests.setUp
    tearDown = fixtures.ServerProviderAdminTests.tearDown

    def save(self, connection_id=None, **patches):
        data = dict(name="分组 B", base_url="https://api.openai.com/group-b/v1", wire_api="chat-completions", api_key="group-b-secret", enabled=True, revision=0)
        data.update(patches)
        with patch("socket.getaddrinfo", side_effect=fixtures.ServerProviderAdminTests.public_resolver):
            return save_connection(self.service, self.admin, connection_id, data)

    def bind(self, connection):
        data = read_catalog(self.sessions)
        data["items"].append(dict(data["items"][0], id="group-b", model="same-upstream-model", connection_id=connection["id"], wire_api="", input_usd_per_million="2"))
        data["default_tier"] = "group-b"
        return save_catalog(self.sessions, self.admin, data)

    def enqueue(self, project, key, **kwargs):
        return WorkflowRepository(self.sessions).create_or_get_job(self.user.user_id, project.project_id, "project", "sections.generate", key, {}, **kwargs)

    def finish(self, job):
        with database_session(self.sessions) as session:
            session.get(WorkflowJob, uuid.UUID(job.id)).status = "failed"

    def test_encrypted_versions_and_blank_key_preservation(self):
        connection = self.save()
        self.save(connection["id"], revision=1, api_key=None, base_url="https://api.openai.com/new/v1")
        old = runtime_for_connection(self.service, connection["id"], 1)
        current = runtime_for_connection(self.service, connection["id"])
        self.assertEqual("group-b-secret", current.api_key)
        self.assertNotEqual(old.base_url, current.base_url)
        with database_session(self.sessions) as session:
            raw = repr(session.get(WorkflowSystemState, CONNECTIONS_KEY).value_json)
        self.assertNotIn("group-b-secret", raw)
        self.assertNotIn("environment-secret", raw)
        result = list_connections(self.service, self.admin)
        self.assertNotIn("encrypted_secret", repr(result))
        self.assertNotIn("group-b-secret", repr(result))
        with self.assertRaises(ProviderSettingsError):
            self.save(connection["id"], revision=1)
        with self.assertRaises(ProviderSettingsError):
            self.save()  # duplicate name
        with self.assertRaises(AuthorizationError):
            list_connections(self.service, self.user)
        with self.assertRaises(AuthorizationError):
            save_connection(self.service, self.user, None, {})

    def test_old_jobs_and_explicit_retries_pin_connection_model_and_prices(self):
        connection = self.save()
        catalog = self.bind(connection)
        project = HostedProjectRepository(self.sessions).create_for_user(self.user.user_id, slug="pinned", topic="test", taxonomy_profile="general_academic", model_tier=None)
        old = self.enqueue(project, "old")
        snapshot = old.payload[SNAPSHOT_KEY]
        self.assertEqual(1, snapshot["connection_revision"])
        self.save(connection["id"], revision=1, api_key="rotated-key", wire_api="responses", base_url="https://api.openai.com/rotated/v1")
        catalog["items"][-1].update(input_usd_per_million="9")
        save_catalog(self.sessions, self.admin, catalog)
        self.finish(old)
        new = self.enqueue(project, "new")
        self.assertEqual(2, new.payload[SNAPSHOT_KEY]["connection_revision"])
        self.assertEqual("9", new.payload[SNAPSHOT_KEY]["input_usd_per_million"])
        self.finish(new)
        self.save(connection["id"], revision=2, enabled=False)
        with self.assertRaises(WorkflowValidationError):
            self.enqueue(project, "disabled")
        retried = self.enqueue(project, "retry", retry_of_job_id=old.id)
        self.assertEqual(snapshot, retried.payload[SNAPSHOT_KEY])
        gateway = ModelGatewayService(self.sessions, self.settings, provider_settings=self.service)
        response = type("Reply", (), {"status_code": 200, "json": lambda _: {"choices": [{"message": {"content": '{"ok":true}'}}]}})()
        try:
            token = gateway.issue_task_token(job_id=retried.id, user_id=self.user.user_id, project_id=project.project_id, job_type="sections.generate")
            tier = gateway._claims_model(gateway.verify_task_token(token))
            with patch.object(gateway._provider_client, "post", new_callable=AsyncMock, return_value=response) as post:
                asyncio.run(gateway._provider_call(tier=tier, prompt="test", idempotency_key="old"))
                self.assertEqual("https://api.openai.com/group-b/v1/chat/completions", post.call_args.args[0])
                self.assertEqual("Bearer group-b-secret", post.call_args.kwargs["headers"]["Authorization"])
                self.assertEqual("same-upstream-model", post.call_args.kwargs["json"]["model"])
        finally:
            asyncio.run(gateway.close())

    def test_admin_routes_and_public_redaction_and_availability(self):
        from fastapi.testclient import TestClient
        from review_writer_api.app import create_app
        connection = self.save()
        self.bind(connection)
        for principal in (self.user, self.admin):
            app = create_app(self.settings, session_factory_override=self.sessions, principal_provider=lambda: principal)
            with TestClient(app) as client:
                catalog = client.get("/api/v1/model-catalog").json()
                self.assertNotIn("connection_id", repr(catalog))
                self.assertNotIn("connection_revision", repr(catalog))
                self.assertNotIn("api_key", repr(catalog))
                self.assertEqual(200 if principal is self.admin else 403, client.get("/api/v1/admin/text-connections").status_code)
                self.assertEqual(200 if principal is self.admin else 403, client.get("/api/v1/admin/model-catalog").status_code)
                if principal is self.admin:
                    data = client.get("/api/v1/admin/model-catalog").json()
                    self.assertEqual(connection["id"], data["items"][-1]["connection_id"])
                    self.assertEqual(200, client.put("/api/v1/admin/model-catalog", json=data).status_code)
        self.save(connection["id"], revision=1, enabled=False)
        from review_writer_api.model_catalog import public_catalog
        self.assertFalse(public_catalog(self.sessions)["items"][-1]["enabled"])

    def test_model_test_uses_bound_connection(self):
        connection = self.save()
        self.bind(connection)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = type("Reply", (), {"status_code": 200, "json": lambda _: {"choices": [{"message": {"content": '{"ok":true}'}}]}})()
        with patch("review_writer_api.server_providers.httpx.AsyncClient", return_value=client):
            result = asyncio.run(self.service.test_connection(self.admin, "text", model_id="group-b"))
        self.assertTrue(result.ok)
        self.assertEqual("https://api.openai.com/group-b/v1/chat/completions", client.post.call_args.args[0])
        self.assertEqual("Bearer group-b-secret", client.post.call_args.kwargs["headers"]["Authorization"])

    def test_two_connections_same_upstream_model_remain_distinct(self):
        first = self.save()
        second = self.save(name="分组 C", api_key="group-c-secret", base_url="https://api.openai.com/group-c/v1")
        catalog = self.bind(first)
        catalog["items"].append(dict(catalog["items"][-1], id="group-c", connection_id=second["id"], input_usd_per_million="4"))
        save_catalog(self.sessions, self.admin, catalog)
        with database_session(self.sessions) as session:
            first_model = model_from_dict(snapshot_for_job(session, "group-b", default_wire="responses"))
            second_model = model_from_dict(snapshot_for_job(session, "group-c", default_wire="responses"))
        self.assertEqual(first_model.model, second_model.model)
        self.assertNotEqual(first_model.input_usd_per_million, second_model.input_usd_per_million)
        gateway = ModelGatewayService(self.sessions, self.settings, provider_settings=self.service)
        try:
            self.assertEqual("group-b-secret", gateway._text_runtime(first_model).api_key)
            self.assertEqual("group-c-secret", gateway._text_runtime(second_model).api_key)
        finally:
            asyncio.run(gateway.close())
        self.save("default", revision=1, name="默认连接", enabled=False)
        status = next(item for item in self.service.list_settings(self.user) if item.provider_kind == "text")
        self.assertTrue(status.enabled)  # Other connections remain usable.

    def test_connection_validation_and_revision_api(self):
        for patches in ({"api_key": ""}, {"base_url": "http://127.0.0.1/v1"}, {"wire_api": "invalid"}):
            with self.subTest(patches=patches), self.assertRaises(ProviderSettingsError):
                self.save(**patches)
        from fastapi.testclient import TestClient
        from review_writer_api.app import create_app
        app = create_app(self.settings, session_factory_override=self.sessions, principal_provider=lambda: self.admin)
        with TestClient(app) as client, patch("socket.getaddrinfo", side_effect=fixtures.ServerProviderAdminTests.public_resolver):
            data = dict(name="API Group", base_url="https://api.openai.com/v1", wire_api="responses", api_key="api-test-secret", enabled=True)
            response = client.post("/api/v1/admin/text-connections", json=data)
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn("api-test-secret", response.text)
            saved = response.json()
            update = dict(data, revision=1, api_key=None, name="Renamed")
            response = client.put(f"/api/v1/admin/text-connections/{saved['id']}", json=update)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(2, response.json()["revision"])
            self.assertEqual(422, client.put(f"/api/v1/admin/text-connections/{saved['id']}", json=update).status_code)
