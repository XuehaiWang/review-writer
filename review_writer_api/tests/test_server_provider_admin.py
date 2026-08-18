from __future__ import annotations

import base64
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.auth import AuthService
from review_writer_api.config import ApiSettings
from review_writer_api.database import (
    Base,
    ServerProviderCredential,
    User,
    database_session,
)
from review_writer_api.security import AuthorizationError, Principal, Role
from review_writer_api.server_providers import ServerProviderSettingsService


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class ServerProviderAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with database_session(self.sessions) as session:
            admin = User(
                email="admin@example.com",
                display_name="Admin",
                password_hash="hash",
                role=Role.ADMIN.value,
            )
            user = User(
                email="user@example.com",
                display_name="User",
                password_hash="hash",
                role=Role.USER.value,
            )
            session.add_all([admin, user])
            session.flush()
            self.admin = Principal(
                str(admin.id), frozenset({Role.ADMIN}), admin.email
            )
            self.user = Principal(str(user.id), frozenset({Role.USER}), user.email)
        self.settings = ApiSettings(
            review_root=Path(self.temporary.name),
            deployment_mode="hosted",
            database_url="sqlite+pysqlite:///:memory:",
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=Path(self.temporary.name) / "users",
            text_provider_api_key="environment-secret",
            text_provider_base_url="https://api.openai.com/v1",
            text_provider_wire_api="responses",
            allowed_provider_hosts=("api.openai.com",),
        )
        self.service = ServerProviderSettingsService(self.settings, self.sessions)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    @staticmethod
    def public_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def test_only_admin_can_update_and_secret_is_encrypted(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.service.save_settings(
                self.user,
                "text",
                base_url="https://api.openai.com/v1",
                model_name="",
                wire_api="responses",
                api_key="database-secret",
                enabled=True,
            )
        with patch(
            "review_writer_api.credentials.socket.getaddrinfo",
            side_effect=self.public_resolver,
        ):
            saved = self.service.save_settings(
                self.admin,
                "text",
                base_url="https://api.openai.com/v1",
                model_name="",
                wire_api="chat-completions",
                api_key="database-secret",
                enabled=True,
            )
        self.assertEqual("database", saved.source)
        self.assertEqual("chat-completions", saved.wire_api)
        self.assertNotIn("database-secret", repr(saved))
        runtime = self.service.runtime_config("text")
        self.assertEqual("database-secret", runtime.api_key)
        with database_session(self.sessions) as session:
            row = session.scalar(select(ServerProviderCredential))
            self.assertIsNotNone(row)
            self.assertNotIn(b"database-secret", row.encrypted_secret)

    def test_reset_immediately_restores_environment_fallback(self) -> None:
        with patch(
            "review_writer_api.credentials.socket.getaddrinfo",
            side_effect=self.public_resolver,
        ):
            self.service.save_settings(
                self.admin,
                "text",
                base_url="https://api.openai.com/v1",
                model_name="",
                wire_api="chat-completions",
                api_key="database-secret",
                enabled=True,
            )
        self.assertEqual("database-secret", self.service.runtime_config("text").api_key)
        restored = self.service.reset_settings(self.admin, "text")
        self.assertEqual("environment", restored.source)
        self.assertEqual("environment-secret", self.service.runtime_config("text").api_key)

    def test_admin_email_bootstraps_role_on_registration(self) -> None:
        auth = AuthService(
            self.sessions,
            admin_emails=("owner@example.com",),
        )
        authenticated = auth.register(
            email="OWNER@example.com",
            password="strong-password-123",
            display_name="Owner",
        )
        self.assertIn(Role.ADMIN, authenticated.principal.roles)


if __name__ == "__main__":
    unittest.main()
