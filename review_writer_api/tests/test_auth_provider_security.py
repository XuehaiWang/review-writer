from __future__ import annotations

import base64
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.auth import AuthAttemptThrottle, AuthRateLimited
from review_writer_api.config import ApiSettings
from review_writer_api.credentials import (
    CredentialCipher,
    ProviderSettingsError,
    ProviderSettingsService,
    validate_provider_base_url,
)
from review_writer_api.database import Base, User, database_session
from review_writer_api.security import Principal, Role


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class AuthProviderSecurityTests(unittest.TestCase):
    @staticmethod
    def resolver(address: str):
        return lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        ]

    def test_hosted_environment_rejects_sqlite_and_retired_local_mode(self) -> None:
        common = {
            "REVIEW_WRITER_PUBLIC_ORIGIN": "http://testserver",
            "REVIEW_WRITER_CREDENTIAL_ENCRYPTION_KEY": TEST_KEY,
            "REVIEW_WRITER_SESSION_COOKIE_SECURE": "false",
        }
        with patch.dict(
            os.environ,
            {
                **common,
                "REVIEW_WRITER_DEPLOYMENT_MODE": "hosted",
                "REVIEW_WRITER_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            },
            clear=True,
        ), self.assertRaisesRegex(ValueError, "PostgreSQL"):
            ApiSettings.from_env(Path.cwd())
        with patch.dict(
            os.environ,
            {**common, "REVIEW_WRITER_DEPLOYMENT_MODE": "local"},
            clear=True,
        ), self.assertRaisesRegex(ValueError, "no longer supported"):
            ApiSettings.from_env(Path.cwd())

    def test_hosted_security_knobs_are_exposed_by_compose(self) -> None:
        root = Path(__file__).resolve().parents[2]
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        example = (root / ".env.hosted.example").read_text(encoding="utf-8")
        for name in (
            "REVIEW_WRITER_AUTH_RATE_LIMIT_ATTEMPTS",
            "REVIEW_WRITER_AUTH_RATE_LIMIT_WINDOW_SECONDS",
            "REVIEW_WRITER_ALLOW_PRIVATE_PROVIDER_URLS",
            "REVIEW_WRITER_ALLOWED_PROVIDER_HOSTS",
            "REVIEW_WRITER_TRUSTED_PROXY_NETWORKS",
        ):
            with self.subTest(name=name):
                self.assertIn(name, compose)
                self.assertIn(name, example)

    def test_public_provider_urls_require_an_administrator_allowlist(self) -> None:
        with self.assertRaisesRegex(ProviderSettingsError, "allowlist"):
            validate_provider_base_url(
                "https://attacker-controlled.example/v1",
                allow_private_urls=False,
                resolver=self.resolver("93.184.216.34"),
            )
        self.assertEqual(
            "https://models.example/v1",
            validate_provider_base_url(
                "https://models.example/v1",
                allow_private_urls=False,
                allowed_hosts={"models.example"},
                resolver=self.resolver("93.184.216.34"),
            ),
        )
        with self.assertRaisesRegex(ProviderSettingsError, "allowlist"):
            validate_provider_base_url(
                "https://models.example.attacker-controlled.example/v1",
                allow_private_urls=False,
                allowed_hosts={"models.example"},
                resolver=self.resolver("93.184.216.34"),
            )

    def test_empty_provider_urls_expand_to_allowlisted_real_destinations(self) -> None:
        with tempfile.TemporaryDirectory():
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            with database_session(sessions) as session:
                user = User(
                    email="provider-owner@example.com",
                    display_name="Provider owner",
                    password_hash="hash",
                )
                session.add(user)
                session.flush()
                # The legacy service remains available only for controlled
                # migration/cleanup tooling and now requires an administrator.
                principal = Principal(str(user.id), frozenset({Role.ADMIN}), user.email)

            service = ProviderSettingsService(
                sessions,
                CredentialCipher(TEST_KEY),
                allowed_hosts={"api.openai.com", "mineru.net"},
            )
            with patch(
                "review_writer_api.credentials.socket.getaddrinfo",
                side_effect=self.resolver("93.184.216.34"),
            ):
                text_record = service.save_settings(
                    principal,
                    "text",
                    base_url="",
                    model_name="gpt-test",
                    wire_api="responses",
                    api_key="text-secret",
                    enabled=True,
                )
                mineru_record = service.save_settings(
                    principal,
                    "mineru",
                    base_url="",
                    model_name="",
                    wire_api="",
                    api_key="mineru-secret",
                    enabled=True,
                )
                with self.assertRaisesRegex(ProviderSettingsError, "fixed"):
                    service.save_settings(
                        principal,
                        "mineru",
                        base_url="https://approved-proxy.example/v1",
                        model_name="",
                        wire_api="",
                        api_key=None,
                        enabled=True,
                    )
                runtime = service.runtime_environment(principal)
            self.assertEqual("https://api.openai.com/v1", text_record.base_url)
            self.assertEqual("https://mineru.net", mineru_record.base_url)
            self.assertEqual(
                "https://api.openai.com/v1", runtime["REVIEW_WRITING_BASE_URL"]
            )

            blocked = ProviderSettingsService(
                sessions,
                CredentialCipher(TEST_KEY),
                allowed_hosts=(),
            )
            with patch(
                "review_writer_api.credentials.socket.getaddrinfo",
                side_effect=self.resolver("93.184.216.34"),
            ), self.assertRaisesRegex(ProviderSettingsError, "allowlist"):
                blocked.save_settings(
                    principal,
                    "image",
                    base_url="",
                    model_name="image-test",
                    wire_api="images",
                    api_key="image-secret",
                    enabled=True,
                )
            engine.dispose()

    def test_legacy_custom_mineru_url_cannot_mask_the_fixed_destination(self) -> None:
        from review_writer_api.credentials import effective_provider_base_url

        self.assertEqual(
            "https://mineru.net", effective_provider_base_url("mineru", "")
        )
        self.assertEqual(
            "https://mineru.net",
            effective_provider_base_url("mineru", "https://mineru.net/"),
        )
        with self.assertRaisesRegex(ProviderSettingsError, "fixed"):
            effective_provider_base_url(
                "mineru", "https://approved-proxy.example/v1"
            )

    def test_auth_throttle_is_bounded_and_success_can_clear_one_key(self) -> None:
        clock = [100.0]
        throttle = AuthAttemptThrottle(
            max_attempts=2, window_seconds=30, max_keys=3, clock=lambda: clock[0]
        )
        throttle.consume("login:127.0.0.1")
        throttle.consume("login:127.0.0.1")
        with self.assertRaises(AuthRateLimited):
            throttle.consume("login:127.0.0.1")
        throttle.clear("login:127.0.0.1")
        throttle.consume("login:127.0.0.1")
        clock[0] += 31
        throttle.consume("login:127.0.0.1")
        for index in range(10):
            throttle.consume(f"high-cardinality:{index}")
        self.assertLessEqual(len(throttle._attempts), 3)

    def test_provider_url_rejects_credentials_private_and_plain_http_by_default(self) -> None:
        for value in (
            "https://user:secret@example.com/v1",
            "https://127.0.0.1:9000/v1",
            "https://192.168.1.9/v1",
            "https://localhost/v1",
            "http://models.example/v1",
            "ftp://models.example/v1",
            "https://models.example/v1?token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(ProviderSettingsError):
                validate_provider_base_url(value, allow_private_urls=False)
        self.assertEqual(
            "https://models.example/v1",
            validate_provider_base_url(
                "https://models.example/v1",
                allow_private_urls=False,
                allowed_hosts={"models.example"},
                resolver=self.resolver("93.184.216.34"),
            ),
        )

    def test_provider_url_rejects_dns_names_resolving_to_private_addresses(self) -> None:
        for hostname, address in (
            ("127.0.0.1.nip.io", "127.0.0.1"),
            ("localtest.me", "127.0.0.1"),
            ("10.0.0.1.sslip.io", "10.0.0.1"),
        ):
            with self.subTest(hostname=hostname), self.assertRaises(ProviderSettingsError):
                validate_provider_base_url(
                    f"https://{hostname}/v1",
                    allow_private_urls=False,
                    resolver=self.resolver(address),
                )

    def test_provider_url_accepts_only_explicit_proxy_networks(self) -> None:
        self.assertEqual(
            "https://mineru.net",
            validate_provider_base_url(
                "https://mineru.net",
                allow_private_urls=False,
                allowed_hosts={"mineru.net"},
                trusted_proxy_networks={"198.18.0.0/15", "fdfe:dcba:9876::/64"},
                resolver=self.resolver("198.18.0.74"),
            ),
        )
        with self.assertRaisesRegex(ProviderSettingsError, "[Pp]rivate"):
            validate_provider_base_url(
                "https://mineru.net",
                allow_private_urls=False,
                allowed_hosts={"mineru.net"},
                trusted_proxy_networks={"198.18.0.0/15"},
                resolver=self.resolver("192.168.0.9"),
            )
        with self.assertRaisesRegex(ProviderSettingsError, "allowlist"):
            validate_provider_base_url(
                "https://not-approved.example",
                allow_private_urls=False,
                allowed_hosts={"mineru.net"},
                trusted_proxy_networks={"198.18.0.0/15"},
                resolver=self.resolver("198.18.0.74"),
            )

    def test_trusted_lan_mode_allows_private_http_but_not_public_http(self) -> None:
        self.assertEqual(
            "http://192.168.0.5:11434/v1",
            validate_provider_base_url(
                "http://192.168.0.5:11434/v1", allow_private_urls=True
            ),
        )
        with self.assertRaises(ProviderSettingsError):
            validate_provider_base_url(
                "http://models.example/v1",
                allow_private_urls=True,
                resolver=self.resolver("93.184.216.34"),
            )

    def test_hosted_login_route_returns_429_after_bounded_failures(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            settings = ApiSettings(
                review_root=Path(raw),
                deployment_mode="hosted",
                database_url="sqlite+pysqlite:///:memory:",
                public_origin="http://testserver",
                credential_encryption_key=TEST_KEY,
                hosted_workspace_root=Path(raw) / "users",
                auth_rate_limit_attempts=2,
                auth_rate_limit_window_seconds=60,
            )
            app = create_app(settings, session_factory_override=sessions)
            with TestClient(app) as client:
                registered = client.post(
                    "/api/v1/auth/register",
                    headers={"Origin": "http://testserver"},
                    json={
                        "email": "chemist@example.com",
                        "password": "strong-password-123",
                        "display_name": "Chemist",
                    },
                )
                self.assertEqual(201, registered.status_code, registered.text)
                client.post(
                    "/api/v1/auth/logout", headers={"Origin": "http://testserver"}
                )
                statuses = [
                    client.post(
                        "/api/v1/auth/login",
                        headers={"Origin": "http://testserver"},
                        json={
                            "email": "chemist@example.com",
                            "password": "wrong-password",
                        },
                    ).status_code
                    for _ in range(3)
                ]
            engine.dispose()
        self.assertEqual([401, 401, 429], statuses)

    def test_successful_account_cannot_clear_another_identity_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            settings = ApiSettings(
                review_root=Path(raw),
                deployment_mode="hosted",
                database_url="sqlite+pysqlite:///:memory:",
                public_origin="http://testserver",
                credential_encryption_key=TEST_KEY,
                hosted_workspace_root=Path(raw) / "users",
                auth_rate_limit_attempts=2,
                auth_rate_limit_window_seconds=60,
            )
            app = create_app(settings, session_factory_override=sessions)
            with TestClient(app) as client:
                for email in ("victim@example.com", "attacker@example.com"):
                    response = client.post(
                        "/api/v1/auth/register",
                        headers={"Origin": "http://testserver"},
                        json={"email": email, "password": "strong-password-123"},
                    )
                    self.assertEqual(201, response.status_code, response.text)
                    client.post("/api/v1/auth/logout", headers={"Origin": "http://testserver"})
                statuses = []
                for email, password in (
                    ("victim@example.com", "wrong-password"),
                    ("attacker@example.com", "strong-password-123"),
                    ("victim@example.com", "wrong-password"),
                    ("victim@example.com", "wrong-password"),
                ):
                    statuses.append(
                        client.post(
                            "/api/v1/auth/login",
                            headers={"Origin": "http://testserver"},
                            json={"email": email, "password": password},
                        ).status_code
                    )
            engine.dispose()
        self.assertEqual([401, 200, 401, 429], statuses)


if __name__ == "__main__":
    unittest.main()
