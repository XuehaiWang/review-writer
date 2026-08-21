from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, PasswordResetToken, database_session


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
ORIGIN = {"Origin": "http://testserver"}


class RecordingMailer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, str, int]] = []

    def send(self, recipient: str, reset_url: str, expires_minutes: int) -> None:
        self.messages.append((recipient, reset_url, expires_minutes))
        if self.fail:
            raise RuntimeError("mail unavailable")


class PasswordResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.settings = ApiSettings(
            review_root=Path(self.temporary.name),
            deployment_mode="hosted",
            database_url="sqlite+pysqlite:///:memory:",
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=Path(self.temporary.name) / "users",
            password_reset_minutes=30,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def client(self, mailer=None) -> TestClient:
        return TestClient(
            create_app(
                self.settings,
                session_factory_override=self.sessions,
                password_reset_mailer_override=mailer,
            )
        )

    def register(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/register",
            headers=ORIGIN,
            json={
                "email": "chemist@example.com",
                "password": "old-password-123",
                "display_name": "Chemist",
            },
        )
        self.assertEqual(201, response.status_code, response.text)

    def test_reset_is_single_use_revokes_sessions_and_hides_account_existence(self) -> None:
        mailer = RecordingMailer()
        with self.client(mailer) as client:
            self.register(client)
            existing = client.post(
                "/api/v1/auth/password-reset/request",
                headers=ORIGIN,
                json={"email": "chemist@example.com"},
            )
            missing = client.post(
                "/api/v1/auth/password-reset/request",
                headers=ORIGIN,
                json={"email": "missing@example.com"},
            )
            self.assertEqual(202, existing.status_code, existing.text)
            self.assertEqual(existing.json(), missing.json())
            self.assertEqual(1, len(mailer.messages))
            recipient, reset_url, minutes = mailer.messages[0]
            self.assertEqual("chemist@example.com", recipient)
            self.assertEqual(30, minutes)
            token = parse_qs(urlparse(reset_url).query)["reset_token"][0]

            with database_session(self.sessions) as database:
                stored = database.scalar(select(PasswordResetToken))
                self.assertIsNotNone(stored)
                self.assertNotEqual(token, stored.token_hash)

            changed = client.post(
                "/api/v1/auth/password-reset/complete",
                headers=ORIGIN,
                json={"token": token, "new_password": "new-password-456"},
            )
            self.assertEqual(200, changed.status_code, changed.text)
            self.assertEqual(401, client.get("/api/v1/me").status_code)
            self.assertEqual(
                401,
                client.post(
                    "/api/v1/auth/login",
                    headers=ORIGIN,
                    json={
                        "email": "chemist@example.com",
                        "password": "old-password-123",
                    },
                ).status_code,
            )
            self.assertEqual(
                200,
                client.post(
                    "/api/v1/auth/login",
                    headers=ORIGIN,
                    json={
                        "email": "chemist@example.com",
                        "password": "new-password-456",
                    },
                ).status_code,
            )
            reused = client.post(
                "/api/v1/auth/password-reset/complete",
                headers=ORIGIN,
                json={"token": token, "new_password": "another-password-789"},
            )
            self.assertEqual(400, reused.status_code, reused.text)

    def test_delivery_failure_invalidates_the_credential(self) -> None:
        mailer = RecordingMailer(fail=True)
        with self.client(mailer) as client:
            self.register(client)
            with patch("review_writer_api.app.LOGGER.exception") as logged:
                response = client.post(
                    "/api/v1/auth/password-reset/request",
                    headers=ORIGIN,
                    json={"email": "chemist@example.com"},
                )
            logged.assert_called_once()
            self.assertEqual(202, response.status_code, response.text)
            token = parse_qs(urlparse(mailer.messages[0][1]).query)["reset_token"][0]
            complete = client.post(
                "/api/v1/auth/password-reset/complete",
                headers=ORIGIN,
                json={"token": token, "new_password": "new-password-456"},
            )
            self.assertEqual(400, complete.status_code, complete.text)

    def test_unconfigured_server_reports_reset_as_unavailable(self) -> None:
        with self.client() as client:
            config = client.get("/api/v1/auth/config")
            self.assertFalse(config.json()["password_reset_enabled"])
            response = client.post(
                "/api/v1/auth/password-reset/request",
                headers=ORIGIN,
                json={"email": "chemist@example.com"},
            )
            self.assertEqual(503, response.status_code, response.text)


if __name__ == "__main__":
    unittest.main()
