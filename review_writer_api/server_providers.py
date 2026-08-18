"""Server-wide provider settings with encrypted, hot-reloadable persistence."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import httpx2 as httpx
from sqlalchemy import delete, select

from review_writer_core.providers import DEFAULT_IMAGE_MODEL

from .config import ApiSettings
from .credentials import (
    CredentialCipher,
    ProviderKind,
    ProviderSettingsError,
    WIRE_APIS,
    _secret_hint,
    effective_provider_base_url,
    validate_provider_base_url,
)
from .database import (
    ServerProviderAuditEvent,
    ServerProviderCredential,
    User,
    database_session,
)
from .model_catalog import DEFAULT_MODEL_TIER, resolve_model_tier
from .security import Permission, Principal


SERVER_CREDENTIAL_SUBJECT = "server-global"


@dataclass(frozen=True)
class ServerProviderRuntime:
    provider_kind: str
    base_url: str
    model_name: str
    wire_api: str
    api_key: str
    enabled: bool
    source: str
    api_key_hint: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ServerProviderStatus:
    provider_kind: str
    base_url: str
    model_name: str
    wire_api: str
    api_key_configured: bool
    api_key_hint: str
    enabled: bool
    source: str = "server"
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ServerProviderAuditRecord:
    id: str
    actor_email: str
    provider_kind: str
    action: str
    summary: str
    created_at: datetime


@dataclass(frozen=True)
class ServerProviderTestResult:
    provider_kind: str
    ok: bool
    status_code: int
    latency_ms: int
    message: str


class ServerProviderSettingsService:
    """Resolve database overrides over immutable environment fallbacks."""

    def __init__(self, settings: ApiSettings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self.cipher = CredentialCipher(settings.credential_encryption_key)

    @staticmethod
    def _kind(raw_kind: ProviderKind | str) -> ProviderKind:
        try:
            return raw_kind if isinstance(raw_kind, ProviderKind) else ProviderKind(
                str(raw_kind or "").strip().casefold()
            )
        except ValueError as exc:
            raise ProviderSettingsError("Provider must be one of: mineru, text, image.") from exc

    def _fallback(self, kind: ProviderKind) -> ServerProviderRuntime:
        if kind is ProviderKind.TEXT:
            tier = resolve_model_tier(DEFAULT_MODEL_TIER)
            secret = self.settings.text_provider_api_key
            return ServerProviderRuntime(
                kind.value, self.settings.text_provider_base_url.rstrip("/"), tier.model,
                self.settings.text_provider_wire_api, secret, bool(secret), "environment",
                _secret_hint(secret) if secret else "",
            )
        if kind is ProviderKind.IMAGE:
            secret = self.settings.image_provider_api_key
            return ServerProviderRuntime(
                kind.value, self.settings.image_provider_base_url.rstrip("/"),
                self.settings.image_provider_model or DEFAULT_IMAGE_MODEL,
                self.settings.image_provider_wire_api, secret, bool(secret), "environment",
                _secret_hint(secret) if secret else "",
            )
        secret = self.settings.mineru_api_token
        return ServerProviderRuntime(
            kind.value, effective_provider_base_url(kind, ""), "", "", secret,
            bool(secret), "environment", _secret_hint(secret) if secret else "",
        )

    def runtime_config(self, provider_kind: ProviderKind | str) -> ServerProviderRuntime:
        """Return the effective secret-bearing configuration for internal use only."""
        kind = self._kind(provider_kind)
        fallback = self._fallback(kind)
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(ServerProviderCredential).where(
                    ServerProviderCredential.provider_kind == kind.value
                )
            )
            if row is None:
                return fallback
            secret = (
                self.cipher.decrypt(SERVER_CREDENTIAL_SUBJECT, kind.value, row.encrypted_secret)
                if row.encrypted_secret else fallback.api_key
            )
            return ServerProviderRuntime(
                provider_kind=kind.value,
                base_url=(row.base_url or fallback.base_url).rstrip("/"),
                model_name=row.model_name or fallback.model_name,
                wire_api=row.wire_api or fallback.wire_api,
                api_key=secret,
                enabled=bool(row.enabled and secret),
                source="database",
                api_key_hint=row.secret_hint or fallback.api_key_hint,
                updated_at=row.updated_at,
            )

    def list_settings(self, principal: Principal | None = None) -> list[ServerProviderStatus]:
        is_admin = bool(principal and Permission.PROVIDER_MANAGE in principal.permissions)
        records: list[ServerProviderStatus] = []
        for kind in ProviderKind:
            runtime = self.runtime_config(kind)
            records.append(ServerProviderStatus(
                provider_kind=kind.value,
                base_url=runtime.base_url if is_admin else "",
                model_name=runtime.model_name,
                wire_api=runtime.wire_api if is_admin else "",
                api_key_configured=bool(runtime.api_key),
                api_key_hint=((runtime.api_key_hint if is_admin else "服务器统一配置")
                              if runtime.api_key else ""),
                enabled=runtime.enabled,
                source=runtime.source if is_admin else "server",
                updated_at=runtime.updated_at if is_admin else None,
            ))
        return records

    def save_settings(
        self, principal: Principal, provider_kind: str, *, base_url: str,
        model_name: str, wire_api: str, api_key: str | None, enabled: bool,
    ) -> ServerProviderStatus:
        principal.require(Permission.PROVIDER_MANAGE)
        kind = self._kind(provider_kind)
        normalized_wire = str(wire_api or "").strip().casefold().replace("_", "-")
        if normalized_wire not in WIRE_APIS[kind]:
            expected = ", ".join(sorted(value for value in WIRE_APIS[kind] if value)) or "blank"
            raise ProviderSettingsError(
                f"Unsupported wire API for {kind.value}; expected {expected}."
            )
        normalized_url = validate_provider_base_url(
            effective_provider_base_url(kind, base_url),
            allow_private_urls=self.settings.allow_private_provider_urls,
            allowed_hosts=self.settings.allowed_provider_hosts,
            trusted_proxy_networks=self.settings.trusted_proxy_networks,
        )
        submitted_key = None if api_key is None else str(api_key).strip()
        fallback = self._fallback(kind)
        actor_id = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            row = session.scalar(select(ServerProviderCredential).where(
                ServerProviderCredential.provider_kind == kind.value
            ))
            if row is None:
                row = ServerProviderCredential(
                    provider_kind=kind.value, encrypted_secret=b"",
                    encryption_key_version=CredentialCipher.VERSION,
                )
                session.add(row)
            if submitted_key:
                row.encrypted_secret = self.cipher.encrypt(
                    SERVER_CREDENTIAL_SUBJECT, kind.value, submitted_key
                )
                row.secret_hint = _secret_hint(submitted_key)
                row.encryption_key_version = CredentialCipher.VERSION
            if enabled and not (submitted_key or row.encrypted_secret or fallback.api_key):
                raise ProviderSettingsError(
                    "An API key is required before this provider can be enabled."
                )
            row.base_url = normalized_url
            row.model_name = str(model_name or "").strip()
            row.wire_api = normalized_wire
            row.enabled = bool(enabled)
            session.flush()
            session.add(ServerProviderAuditEvent(
                actor_user_id=actor_id, provider_kind=kind.value, action="update",
                summary=f"Updated {kind.value} provider; enabled={bool(enabled)}.",
            ))
        return next(item for item in self.list_settings(principal)
                    if item.provider_kind == kind.value)

    def reset_settings(self, principal: Principal, provider_kind: str) -> ServerProviderStatus:
        principal.require(Permission.PROVIDER_MANAGE)
        kind = self._kind(provider_kind)
        with database_session(self.session_factory) as session:
            session.execute(delete(ServerProviderCredential).where(
                ServerProviderCredential.provider_kind == kind.value
            ))
            session.add(ServerProviderAuditEvent(
                actor_user_id=uuid.UUID(principal.user_id), provider_kind=kind.value,
                action="reset", summary=f"Reset {kind.value} provider to environment fallback.",
            ))
        return next(item for item in self.list_settings(principal)
                    if item.provider_kind == kind.value)

    def audit_log(self, principal: Principal, *, limit: int = 50) -> list[ServerProviderAuditRecord]:
        principal.require(Permission.PROVIDER_MANAGE)
        with database_session(self.session_factory) as session:
            rows = session.execute(
                select(ServerProviderAuditEvent, User.email)
                .join(User, User.id == ServerProviderAuditEvent.actor_user_id)
                .order_by(ServerProviderAuditEvent.created_at.desc())
                .limit(max(1, min(int(limit), 200)))
            ).all()
            return [ServerProviderAuditRecord(
                id=str(event.id), actor_email=str(email or ""),
                provider_kind=event.provider_kind, action=event.action,
                summary=event.summary, created_at=event.created_at,
            ) for event, email in rows]

    def _record_test(self, principal: Principal, kind: ProviderKind, *, ok: bool, summary: str) -> None:
        with database_session(self.session_factory) as session:
            session.add(ServerProviderAuditEvent(
                actor_user_id=uuid.UUID(principal.user_id), provider_kind=kind.value,
                action="test_success" if ok else "test_failed", summary=summary[:500],
            ))

    async def test_connection(
        self, principal: Principal, provider_kind: str
    ) -> ServerProviderTestResult:
        principal.require(Permission.PROVIDER_MANAGE)
        kind = self._kind(provider_kind)
        runtime = self.runtime_config(kind)
        if not runtime.enabled:
            raise ProviderSettingsError("The provider is disabled or has no API key.")
        endpoint = runtime.base_url if kind is ProviderKind.MINERU else f"{runtime.base_url.rstrip('/')}/models"
        started = time.perf_counter()
        status_code = 0
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=True) as client:
                response = await client.get(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {runtime.api_key}",
                        "Accept": "application/json",
                        "User-Agent": "ReviewWriter-ProviderCheck/1.0",
                    },
                    follow_redirects=True,
                )
                status_code = int(response.status_code)
            ok = status_code not in {401, 403, 407, 429} and status_code < 500
            message = ("Provider connection succeeded." if ok else
                       f"Provider rejected the connection with HTTP {status_code}.")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            ok = False
            message = f"Provider transport failed: {exc.__class__.__name__}."
        latency = max(0, int((time.perf_counter() - started) * 1000))
        self._record_test(principal, kind, ok=ok, summary=f"{message} latency={latency}ms")
        return ServerProviderTestResult(
            provider_kind=kind.value, ok=ok, status_code=status_code,
            latency_ms=latency, message=message,
        )

    def runtime_environment(
        self, _principal=None, *,
        provider_kinds: Iterable[ProviderKind | str] | None = None,
    ) -> dict[str, str]:
        selected = ({self._kind(item).value for item in provider_kinds}
                    if provider_kinds is not None else {item.value for item in ProviderKind})
        environment: dict[str, str] = {}
        if ProviderKind.MINERU.value in selected:
            runtime = self.runtime_config(ProviderKind.MINERU)
            if runtime.enabled:
                environment["MINERU_API_TOKEN"] = runtime.api_key
        if ProviderKind.TEXT.value in selected:
            runtime = self.runtime_config(ProviderKind.TEXT)
            if runtime.enabled:
                default_model = resolve_model_tier(DEFAULT_MODEL_TIER).model
                environment.update({
                    "OPENAI_API_KEY": runtime.api_key,
                    "OPENAI_BASE_URL": runtime.base_url,
                    "REVIEW_WRITING_API_KEY": runtime.api_key,
                    "REVIEW_WRITING_BASE_URL": runtime.base_url,
                    "REVIEW_WRITING_WIRE_API": runtime.wire_api,
                    "REVIEW_WRITING_MODEL": default_model,
                    "REVIEW_CONCLUSION_API_KEY": runtime.api_key,
                    "REVIEW_CONCLUSION_BASE_URL": runtime.base_url,
                    "REVIEW_CONCLUSION_WIRE_API": runtime.wire_api,
                    "REVIEW_CONCLUSION_MODEL": default_model,
                })
        if ProviderKind.IMAGE.value in selected:
            runtime = self.runtime_config(ProviderKind.IMAGE)
            if runtime.enabled:
                environment.update({
                    "IMAGE_OPENAI_API_KEY": runtime.api_key,
                    "IMAGE_OPENAI_BASE_URL": runtime.base_url,
                    "IMAGE_OPENAI_MODEL": runtime.model_name or DEFAULT_IMAGE_MODEL,
                    "IMAGE_FALLBACK_MODEL": runtime.model_name or DEFAULT_IMAGE_MODEL,
                    "IMAGE_OPENAI_WIRE_API": runtime.wire_api,
                })
        return environment

    def mineru_environment(self, principal=None) -> dict[str, str]:
        return self.runtime_environment(principal, provider_kinds=(ProviderKind.MINERU,))
