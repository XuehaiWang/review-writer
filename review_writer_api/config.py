"""Environment configuration for the compact FastAPI/PostgreSQL application."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy.engine import URL

from review_writer_core.workspace import discover_review_root


DeploymentMode = Literal["local", "hosted"]
COOKIE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ApiSettings:
    review_root: Path
    deployment_mode: DeploymentMode = "local"
    database_url: str = ""
    public_origin: str = ""
    credential_encryption_key: str = ""
    session_cookie_name: str = "review_writer_session"
    session_days: int = 7
    session_cookie_secure: bool = False
    expose_api_docs: bool = True
    hosted_workspace_root: Path | None = None

    @classmethod
    def from_env(cls, review_root: str | Path | None = None) -> "ApiSettings":
        root = Path(review_root).expanduser().resolve() if review_root else discover_review_root()
        raw_mode = str(os.environ.get("REVIEW_WRITER_DEPLOYMENT_MODE") or "local").strip().casefold()
        if raw_mode not in {"local", "hosted"}:
            raise ValueError("REVIEW_WRITER_DEPLOYMENT_MODE must be 'local' or 'hosted'.")

        database_url = database_url_from_env()
        public_origin = str(os.environ.get("REVIEW_WRITER_PUBLIC_ORIGIN") or "").strip().rstrip("/")
        credential_key = str(
            os.environ.get("REVIEW_WRITER_CREDENTIAL_ENCRYPTION_KEY") or ""
        ).strip()
        cookie_name = str(
            os.environ.get("REVIEW_WRITER_SESSION_COOKIE_NAME") or "review_writer_session"
        ).strip()
        session_days = _environment_integer("REVIEW_WRITER_SESSION_DAYS", 7, minimum=1, maximum=30)
        cookie_secure = _environment_flag(
            "REVIEW_WRITER_SESSION_COOKIE_SECURE", raw_mode == "hosted"
        )
        expose_docs = _environment_flag("REVIEW_WRITER_EXPOSE_API_DOCS", raw_mode == "local")
        raw_workspace_root = str(
            os.environ.get("REVIEW_WRITER_HOSTED_WORKSPACE_ROOT") or ""
        ).strip()
        hosted_workspace_root = (
            Path(raw_workspace_root).expanduser().resolve()
            if raw_workspace_root
            else (root / ".review-writer" / "hosted-workspaces").resolve()
        )

        if not COOKIE_NAME_PATTERN.fullmatch(cookie_name):
            raise ValueError("REVIEW_WRITER_SESSION_COOKIE_NAME contains unsupported characters.")
        if raw_mode == "hosted":
            if not database_url or not public_origin:
                raise ValueError(
                    "Hosted mode requires PostgreSQL connection settings and "
                    "REVIEW_WRITER_PUBLIC_ORIGIN."
                )
            parsed_origin = urlparse(public_origin)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.netloc
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
                or parsed_origin.username
                or parsed_origin.password
            ):
                raise ValueError("REVIEW_WRITER_PUBLIC_ORIGIN must be a bare http(s) origin.")
            if cookie_secure and parsed_origin.scheme != "https":
                raise ValueError(
                    "Secure session cookies require an HTTPS REVIEW_WRITER_PUBLIC_ORIGIN."
                )
            _validate_credential_key(credential_key)

        return cls(
            review_root=root,
            deployment_mode=raw_mode,  # type: ignore[arg-type]
            database_url=database_url,
            public_origin=public_origin,
            credential_encryption_key=credential_key,
            session_cookie_name=cookie_name,
            session_days=session_days,
            session_cookie_secure=cookie_secure,
            expose_api_docs=expose_docs,
            hosted_workspace_root=hosted_workspace_root,
        )


def database_url_from_env() -> str:
    """Return an explicit database URL or build one from simple PostgreSQL variables."""

    explicit_url = str(os.environ.get("REVIEW_WRITER_DATABASE_URL") or "").strip()
    if explicit_url:
        return explicit_url

    password = str(os.environ.get("REVIEW_WRITER_POSTGRES_PASSWORD") or "")
    if not password:
        return ""
    host = str(os.environ.get("REVIEW_WRITER_POSTGRES_HOST") or "127.0.0.1").strip()
    username = str(os.environ.get("REVIEW_WRITER_POSTGRES_USER") or "review_writer").strip()
    database = str(os.environ.get("REVIEW_WRITER_POSTGRES_DB") or "review_writer").strip()
    if not host or not username or not database:
        raise ValueError("PostgreSQL host, user, and database name cannot be empty.")
    port = _environment_integer("REVIEW_WRITER_POSTGRES_PORT", 5432, minimum=1, maximum=65535)
    return URL.create(
        "postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    ).render_as_string(hide_password=False)


def _validate_credential_key(encoded_key: str) -> None:
    try:
        padded = encoded_key + "=" * (-len(encoded_key) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(
            "REVIEW_WRITER_CREDENTIAL_ENCRYPTION_KEY must be URL-safe base64."
        ) from exc
    if len(decoded) != 32:
        raise ValueError(
            "Hosted mode requires REVIEW_WRITER_CREDENTIAL_ENCRYPTION_KEY containing "
            "exactly 32 random bytes encoded as URL-safe base64."
        )


def _environment_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name) or "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _environment_integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value
