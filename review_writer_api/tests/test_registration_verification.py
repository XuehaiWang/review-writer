from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.auth import AuthError, AuthService, AuthRateLimited
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, RegistrationCode, User, UserSession, database_session, utc_now
from review_writer_api.password_reset_mailer import SmtpPasswordResetMailer

ORIGIN = {"Origin": "http://testserver"}
EMAIL = "new@example.com"
PASSWORD = "safe-password-123"


class Mailer:
    def __init__(self):
        self.codes = []
        self.fail = False

    def send_registration_code(self, email, code, minutes):
        self.codes.append((email, code, minutes))
        if self.fail:
            raise RuntimeError("SMTP unavailable")


@pytest.fixture
def setup(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = ApiSettings(review_root=tmp_path, deployment_mode="hosted", database_url="sqlite://",
                           public_origin="http://testserver", hosted_workspace_root=tmp_path / "users",
                           credential_encryption_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
    mailer = Mailer()
    app = create_app(settings, session_factory_override=sessions, password_reset_mailer_override=mailer)
    with TestClient(app) as client:
        yield client, app.state.auth_service, sessions, mailer
    engine.dispose()


def register(client, code="", email=EMAIL):
    return client.post("/api/v1/auth/register", headers=ORIGIN,
                       json={"email": email, "password": PASSWORD, "verification_code": code})


def test_email_must_be_verified_before_account_or_session_exists(setup):
    client, auth, sessions, mailer = setup
    assert register(client).status_code == 409
    response = client.post("/api/v1/auth/registration-code", headers=ORIGIN, json={"email": EMAIL.upper()})
    assert response.status_code == 202
    email, code, minutes = mailer.codes[-1]
    assert email == EMAIL and len(code) == 6 and minutes == 10
    assert code not in response.text and "set-cookie" not in response.headers
    with database_session(sessions) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0
        assert db.get(RegistrationCode, EMAIL).code_hash != code
    assert register(client, code, "other@example.com").status_code == 409
    result = register(client, code, EMAIL.upper())
    assert result.status_code == 201 and "set-cookie" in result.headers
    assert result.json()["email"] == EMAIL
    assert register(client, code).status_code == 409
    assert client.post("/api/v1/auth/login", headers=ORIGIN,
                       json={"email": EMAIL, "password": PASSWORD}).status_code == 200


def test_attempt_limit_persists_across_service_instances(setup):
    _, auth, sessions, _ = setup
    code = auth.issue_registration_code(email=EMAIL)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(AuthError):
            auth.register(email=EMAIL, password=PASSWORD, display_name="", verification_code=wrong)
    with pytest.raises(AuthError):
        AuthService(sessions).register(email=EMAIL, password=PASSWORD, display_name="", verification_code=code)
    with database_session(sessions) as db:
        assert db.get(RegistrationCode, EMAIL).attempts == 5
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_expiry_resend_and_single_use(setup):
    _, auth, sessions, _ = setup
    with patch("review_writer_api.auth.secrets.randbelow", side_effect=[123456, 234567, 345678]):
        first = auth.issue_registration_code(email=EMAIL)
        with pytest.raises(AuthRateLimited):
            auth.issue_registration_code(email=EMAIL)
        with database_session(sessions) as db:
            row = db.get(RegistrationCode, EMAIL)
            row.sent_at = utc_now() - timedelta(minutes=11)
            row.expires_at = utc_now() - timedelta(minutes=1)
        with pytest.raises(AuthError):
            auth.register(email=EMAIL, password=PASSWORD, display_name="", verification_code=first)
        second = auth.issue_registration_code(email=EMAIL)
    with pytest.raises(AuthError):
        auth.register(email=EMAIL, password=PASSWORD, display_name="", verification_code=first)
    assert auth.register(email=EMAIL, password=PASSWORD, display_name="", verification_code=second)


def test_mail_failure_invalidates_code_and_no_account_is_created(setup):
    client, _, sessions, mailer = setup
    mailer.fail = True
    response = client.post("/api/v1/auth/registration-code", headers=ORIGIN, json={"email": EMAIL})
    assert response.status_code == 503
    assert register(client, mailer.codes[-1][1]).status_code == 409
    with database_session(sessions) as db:
        assert db.get(RegistrationCode, EMAIL).used_at is not None
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_resend_route_has_persistent_email_cooldown(setup):
    client, _, _, mailer = setup
    for expected in (202, 429):
        response = client.post("/api/v1/auth/registration-code", headers=ORIGIN, json={"email": EMAIL})
        assert response.status_code == expected
    assert response.headers["Retry-After"] == "60"
    assert len(mailer.codes) == 1


def test_existing_accounts_do_not_require_reverification(setup):
    client, auth, sessions, _ = setup
    with database_session(sessions) as db:
        db.add(User(email="legacy@example.com", display_name="Legacy", password_hash=auth.passwords.hash(PASSWORD), role="user", status="active"))
    response = client.post("/api/v1/auth/login", headers=ORIGIN,
                           json={"email": "legacy@example.com", "password": PASSWORD})
    assert response.status_code == 200


def test_missing_smtp_disables_registration_without_affecting_login(setup):
    client, _, sessions, _ = setup
    app = create_app(client.app.state.settings, session_factory_override=sessions)
    with TestClient(app) as unconfigured:
        assert unconfigured.get("/api/v1/auth/config").json()["registration_enabled"] is False
        assert unconfigured.post("/api/v1/auth/registration-code", headers=ORIGIN,
                                 json={"email": EMAIL}).status_code == 503
        assert register(unconfigured).status_code == 409


def test_account_emails_share_the_same_smtp_delivery():
    mailer = SmtpPasswordResetMailer(host="mail.example.com", port=587, username="u", password="p", from_email="app@example.com")
    with patch.object(mailer, "_deliver") as deliver:
        mailer.send_registration_code(EMAIL, "123456", 10)
        mailer.send(EMAIL, "https://example.com/login?reset_token=token", 30)
    verification, reset = [call.args[0] for call in deliver.call_args_list]
    assert "123456" in verification.get_content() and "注册" in verification["Subject"]
    assert "reset_token=token" in reset.get_content() and "重置" in reset["Subject"]


def test_mail_failure_logs_classification_without_private_data(setup, caplog):
    from smtplib import SMTPDataError
    client, _, _, mailer = setup
    with patch.object(mailer, "send_registration_code", side_effect=SMTPDataError(554, b"private-recipient secret-code")):
        response = client.post("/api/v1/auth/registration-code", headers=ORIGIN, json={"email": EMAIL})
    assert response.status_code == 503
    assert "error_type=SMTPDataError smtp_code=554" in caplog.text
    assert "private-recipient" not in caplog.text
    assert "secret-code" not in caplog.text
    assert EMAIL not in caplog.text
