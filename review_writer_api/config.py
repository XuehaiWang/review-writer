"""Environment configuration for the compact FastAPI/PostgreSQL application."""

from __future__ import annotations

import base64
import ipaddress
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy.engine import URL, make_url

from review_writer_core.workspace import discover_review_root


DeploymentMode = Literal["local", "hosted"]
COOKIE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class RetrievalTuning:
    """Central conservative retrieval limits; intentionally not env-per-knob."""

    chunk_min_tokens: int = 70
    chunk_max_tokens: int = 360
    oversized_overlap_tokens: int = 100
    subsection_top_k: int = 12
    subsection_per_paper_limit: int = 4
    rrf_constant: int = 60


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
    job_worker_count: int = 2
    auth_rate_limit_attempts: int = 12
    auth_rate_limit_window_seconds: int = 60
    allow_private_provider_urls: bool = False
    allowed_provider_hosts: tuple[str, ...] = ()
    trusted_proxy_networks: tuple[str, ...] = ()
    admin_emails: tuple[str, ...] = ()
    text_provider_api_key: str = ""
    text_provider_base_url: str = "https://api.openai.com/v1"
    text_provider_wire_api: str = "responses"
    image_provider_api_key: str = ""
    image_provider_base_url: str = "https://api.openai.com/v1"
    image_provider_model: str = ""
    image_provider_wire_api: str = "images"
    image_provider_price_usd_per_image: Decimal = Decimal("0")
    mineru_api_token: str = ""
    mineru_price_usd_per_page: Decimal = Decimal("0")
    mineru_max_concurrency: int = 2
    internal_gateway_url: str = ""
    model_gateway_max_concurrency: int = 2
    model_gateway_user_concurrency: int = 1
    image_gateway_max_concurrency: int = 1
    image_gateway_user_concurrency: int = 1
    document_retrieval_enabled: bool = True
    vector_retrieval_enabled: bool = False
    retrieval_tuning: RetrievalTuning = field(default_factory=RetrievalTuning)

    @classmethod
    def from_env(cls, review_root: str | Path | None = None) -> "ApiSettings":
        root = Path(review_root).expanduser().resolve() if review_root else discover_review_root()
        raw_mode = str(os.environ.get("REVIEW_WRITER_DEPLOYMENT_MODE") or "hosted").strip().casefold()
        if raw_mode not in {"local", "hosted"}:
            raise ValueError("REVIEW_WRITER_DEPLOYMENT_MODE must be 'local' or 'hosted'.")
        if raw_mode == "local":
            raise ValueError(
                "Local workflow mode is no longer supported; use hosted mode with PostgreSQL."
            )

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
        job_worker_count = _environment_integer(
            "REVIEW_WRITER_JOB_WORKERS", 2, minimum=1, maximum=16
        )
        auth_rate_limit_attempts = _environment_integer(
            "REVIEW_WRITER_AUTH_RATE_LIMIT_ATTEMPTS", 12, minimum=2, maximum=100
        )
        auth_rate_limit_window_seconds = _environment_integer(
            "REVIEW_WRITER_AUTH_RATE_LIMIT_WINDOW_SECONDS",
            60,
            minimum=10,
            maximum=3600,
        )
        allow_private_provider_urls = _environment_flag(
            "REVIEW_WRITER_ALLOW_PRIVATE_PROVIDER_URLS", False
        )
        allowed_provider_hosts = _environment_hosts("REVIEW_WRITER_ALLOWED_PROVIDER_HOSTS")
        trusted_proxy_networks = _environment_networks(
            "REVIEW_WRITER_TRUSTED_PROXY_NETWORKS"
        )
        admin_emails = _environment_emails("REVIEW_WRITER_ADMIN_EMAILS")
        text_provider_api_key = str(
            os.environ.get("REVIEW_WRITER_OPENAI_API_KEY")
            or os.environ.get("REVIEW_WRITING_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        text_provider_base_url = str(
            os.environ.get("REVIEW_WRITER_OPENAI_BASE_URL")
            or os.environ.get("REVIEW_WRITING_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip().rstrip("/")
        text_provider_wire_api = str(
            os.environ.get("REVIEW_WRITER_OPENAI_WIRE_API")
            or os.environ.get("REVIEW_WRITING_WIRE_API")
            or "responses"
        ).strip().casefold().replace("_", "-")
        image_provider_api_key = str(
            os.environ.get("REVIEW_WRITER_IMAGE_API_KEY")
            or os.environ.get("IMAGE_OPENAI_API_KEY")
            or text_provider_api_key
            or ""
        ).strip()
        image_provider_base_url = str(
            os.environ.get("REVIEW_WRITER_IMAGE_BASE_URL")
            or os.environ.get("IMAGE_OPENAI_BASE_URL")
            or text_provider_base_url
        ).strip().rstrip("/")
        image_provider_model = str(
            os.environ.get("REVIEW_WRITER_IMAGE_MODEL")
            or os.environ.get("IMAGE_OPENAI_MODEL")
            or ""
        ).strip()
        image_provider_wire_api = str(
            os.environ.get("REVIEW_WRITER_IMAGE_WIRE_API")
            or os.environ.get("IMAGE_OPENAI_WIRE_API")
            or "images"
        ).strip().casefold().replace("_", "-")
        image_provider_price_usd_per_image = _environment_decimal(
            "REVIEW_WRITER_IMAGE_USD_PER_IMAGE", "0"
        )
        mineru_api_token = str(
            os.environ.get("REVIEW_WRITER_MINERU_API_TOKEN")
            or os.environ.get("MINERU_API_TOKEN")
            or ""
        ).strip()
        mineru_price_usd_per_page = _environment_decimal(
            "REVIEW_WRITER_MINERU_USD_PER_PAGE", "0"
        )
        mineru_max_concurrency = _environment_integer(
            "REVIEW_WRITER_MINERU_CONCURRENCY", 2, minimum=1, maximum=8
        )
        internal_gateway_url = str(
            os.environ.get("REVIEW_WRITER_INTERNAL_GATEWAY_URL")
            or f"{public_origin}/api/internal/v1/model-responses"
        ).strip()
        model_gateway_max_concurrency = _environment_integer(
            "REVIEW_WRITER_MODEL_GATEWAY_CONCURRENCY", 2, minimum=1, maximum=32
        )
        model_gateway_user_concurrency = _environment_integer(
            "REVIEW_WRITER_MODEL_GATEWAY_USER_CONCURRENCY", 1, minimum=1, maximum=8
        )
        image_gateway_max_concurrency = _environment_integer(
            "REVIEW_WRITER_IMAGE_GATEWAY_CONCURRENCY", 1, minimum=1, maximum=8
        )
        image_gateway_user_concurrency = _environment_integer(
            "REVIEW_WRITER_IMAGE_GATEWAY_USER_CONCURRENCY", 1, minimum=1, maximum=4
        )
        document_retrieval_enabled = _environment_flag(
            "REVIEW_DOCUMENT_RETRIEVAL_ENABLED", True
        )
        vector_retrieval_enabled = _environment_flag(
            "REVIEW_VECTOR_RETRIEVAL_ENABLED", False
        )
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
            if make_url(database_url).get_backend_name() != "postgresql":
                raise ValueError("Hosted mode requires a PostgreSQL database URL.")
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
            job_worker_count=job_worker_count,
            auth_rate_limit_attempts=auth_rate_limit_attempts,
            auth_rate_limit_window_seconds=auth_rate_limit_window_seconds,
            allow_private_provider_urls=allow_private_provider_urls,
            allowed_provider_hosts=allowed_provider_hosts,
            trusted_proxy_networks=trusted_proxy_networks,
            admin_emails=admin_emails,
            text_provider_api_key=text_provider_api_key,
            text_provider_base_url=text_provider_base_url,
            text_provider_wire_api=text_provider_wire_api,
            image_provider_api_key=image_provider_api_key,
            image_provider_base_url=image_provider_base_url,
            image_provider_model=image_provider_model,
            image_provider_wire_api=image_provider_wire_api,
            image_provider_price_usd_per_image=image_provider_price_usd_per_image,
            mineru_api_token=mineru_api_token,
            mineru_price_usd_per_page=mineru_price_usd_per_page,
            mineru_max_concurrency=mineru_max_concurrency,
            internal_gateway_url=internal_gateway_url,
            model_gateway_max_concurrency=model_gateway_max_concurrency,
            model_gateway_user_concurrency=model_gateway_user_concurrency,
            image_gateway_max_concurrency=image_gateway_max_concurrency,
            image_gateway_user_concurrency=image_gateway_user_concurrency,
            document_retrieval_enabled=document_retrieval_enabled,
            vector_retrieval_enabled=vector_retrieval_enabled,
        )


def _environment_decimal(name: str, default: str) -> Decimal:
    raw = str(os.environ.get(name) or default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number.") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative.")
    return value.quantize(Decimal("0.00000001"))


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


def _environment_hosts(name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in str(os.environ.get(name) or "").split(","):
        host = raw.strip().casefold().rstrip(".")
        if not host:
            continue
        if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
            raise ValueError(f"{name} must contain comma-separated DNS hostnames.")
        values.append(host)
    return tuple(dict.fromkeys(values))


def _environment_emails(name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in str(os.environ.get(name) or "").split(","):
        value = raw.strip().casefold()
        if not value:
            continue
        if (
            len(value) > 320
            or value.count("@") != 1
            or value.startswith("@")
            or value.endswith("@")
        ):
            raise ValueError(f"{name} must contain comma-separated email addresses.")
        values.append(value)
    return tuple(dict.fromkeys(values))


def _environment_networks(name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in str(os.environ.get(name) or "").split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(
                f"{name} must contain comma-separated canonical IP networks."
            ) from exc
        values.append(str(network))
    return tuple(dict.fromkeys(values))
