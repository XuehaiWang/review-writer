"""Per-user provider settings with authenticated encryption for API secrets."""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select

from .database import ProviderCredential, database_session
from .security import Permission, Principal
from review_writer_core.providers import DEFAULT_OPENAI_BASE_URL


class ProviderKind(StrEnum):
    MINERU = "mineru"
    TEXT = "text"
    IMAGE = "image"


WIRE_APIS: dict[ProviderKind, frozenset[str]] = {
    ProviderKind.MINERU: frozenset({""}),
    ProviderKind.TEXT: frozenset({"chat-completions", "responses"}),
    ProviderKind.IMAGE: frozenset({"images", "chat-completions"}),
}
DEFAULT_PROVIDER_BASE_URLS: dict[ProviderKind, str] = {
    ProviderKind.MINERU: "https://mineru.net",
    ProviderKind.TEXT: DEFAULT_OPENAI_BASE_URL,
    ProviderKind.IMAGE: DEFAULT_OPENAI_BASE_URL,
}


class ProviderSettingsError(ValueError):
    pass


def effective_provider_base_url(provider_kind: ProviderKind | str, raw_url: str) -> str:
    try:
        kind = (
            provider_kind
            if isinstance(provider_kind, ProviderKind)
            else ProviderKind(str(provider_kind or "").strip().casefold())
        )
    except ValueError as exc:
        raise ProviderSettingsError("Provider must be one of: mineru, text, image.") from exc
    submitted = str(raw_url or "").strip().rstrip("/")
    default = DEFAULT_PROVIDER_BASE_URLS[kind]
    if kind is ProviderKind.MINERU:
        if submitted and submitted != default:
            raise ProviderSettingsError(
                "MinerU uses a fixed https://mineru.net provider URL."
            )
        return default
    return submitted or default


def validate_provider_base_url(
    raw_url: str,
    *,
    allow_private_urls: bool,
    allowed_hosts=(),
    resolver=None,
) -> str:
    value = str(raw_url or "").strip().rstrip("/")
    if not value:
        raise ProviderSettingsError("Provider base URL is required.")
    if any(ord(character) < 32 or character.isspace() for character in value):
        raise ProviderSettingsError("Provider base URL contains unsupported whitespace.")
    try:
        parsed = urlsplit(value)
        hostname = str(parsed.hostname or "").casefold().rstrip(".")
        _port = parsed.port
    except ValueError as exc:
        raise ProviderSettingsError("Provider base URL is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderSettingsError(
            "Provider base URL must be an http(s) URL without credentials, query, or fragment."
        )
    private = hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal")
    )
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        private = True
    if address is None and not private:
        resolve = resolver or socket.getaddrinfo
        try:
            answers = resolve(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {
                ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
                for answer in answers
            }
        except (OSError, ValueError, IndexError, TypeError) as exc:
            raise ProviderSettingsError("Provider hostname could not be safely resolved.") from exc
        if not addresses:
            raise ProviderSettingsError("Provider hostname could not be safely resolved.")
        private = any(not item.is_global for item in addresses)
    if private and not allow_private_urls:
        raise ProviderSettingsError(
            "Private or loopback provider URLs require trusted-LAN mode."
        )
    normalized_allowed_hosts = {
        str(item or "").strip().casefold().rstrip(".") for item in allowed_hosts
    }
    if not private and hostname not in normalized_allowed_hosts:
        raise ProviderSettingsError(
            "Public provider hostname is not in the administrator allowlist."
        )
    if parsed.scheme == "http" and not (allow_private_urls and private):
        raise ProviderSettingsError(
            "Plain HTTP is allowed only for a private provider in trusted-LAN mode."
        )
    return value


@dataclass(frozen=True)
class ProviderSettingsRecord:
    provider_kind: str
    base_url: str
    model_name: str
    wire_api: str
    api_key_configured: bool
    api_key_hint: str
    enabled: bool


def _decode_key(encoded_key: str) -> bytes:
    try:
        padded = encoded_key + "=" * (-len(encoded_key) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ProviderSettingsError("Credential encryption key is invalid.") from exc
    if len(key) != 32:
        raise ProviderSettingsError("Credential encryption key must contain 32 bytes.")
    return key


class CredentialCipher:
    """AES-256-GCM with user/provider-bound additional authenticated data."""

    VERSION = "aes256gcm-v1"
    PREFIX = b"RW1"

    def __init__(self, encoded_key: str):
        self._cipher = AESGCM(_decode_key(encoded_key))

    @classmethod
    def _aad(cls, user_id: str, provider_kind: str) -> bytes:
        return f"review-writer|{user_id}|{provider_kind}|{cls.VERSION}".encode("utf-8")

    def encrypt(self, user_id: str, provider_kind: str, secret: str) -> bytes:
        value = str(secret or "")
        if not value:
            raise ProviderSettingsError("A non-empty API key is required.")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            value.encode("utf-8"),
            self._aad(user_id, provider_kind),
        )
        return self.PREFIX + nonce + ciphertext

    def decrypt(self, user_id: str, provider_kind: str, payload: bytes) -> str:
        encoded = bytes(payload or b"")
        if not encoded.startswith(self.PREFIX) or len(encoded) <= len(self.PREFIX) + 12:
            raise ProviderSettingsError("Stored provider credential has an unsupported format.")
        offset = len(self.PREFIX)
        nonce = encoded[offset : offset + 12]
        ciphertext = encoded[offset + 12 :]
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._aad(user_id, provider_kind),
            )
        except Exception as exc:
            raise ProviderSettingsError("Stored provider credential could not be decrypted.") from exc
        return plaintext.decode("utf-8")


def _secret_hint(secret: str) -> str:
    value = str(secret or "")
    if len(value) <= 4:
        return "•" * len(value)
    return f"••••{value[-4:]}"


class ProviderSettingsService:
    def __init__(
        self,
        session_factory,
        cipher: CredentialCipher,
        *,
        allow_private_urls: bool = False,
        allowed_hosts=(),
    ):
        self.session_factory = session_factory
        self.cipher = cipher
        self.allow_private_urls = bool(allow_private_urls)
        self.allowed_hosts = tuple(allowed_hosts)

    @staticmethod
    def _kind(raw_kind: str) -> ProviderKind:
        try:
            return ProviderKind(str(raw_kind or "").strip().casefold())
        except ValueError as exc:
            raise ProviderSettingsError("Provider must be one of: mineru, text, image.") from exc

    @staticmethod
    def _record(row: ProviderCredential) -> ProviderSettingsRecord:
        return ProviderSettingsRecord(
            provider_kind=row.provider_kind,
            base_url=effective_provider_base_url(row.provider_kind, row.base_url),
            model_name=row.model_name,
            wire_api=row.wire_api,
            api_key_configured=bool(row.encrypted_secret),
            api_key_hint=row.secret_hint,
            enabled=row.enabled,
        )

    def list_settings(self, principal: Principal) -> list[ProviderSettingsRecord]:
        principal.require(Permission.PROVIDER_MANAGE)
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            rows = session.scalars(
                select(ProviderCredential)
                .where(ProviderCredential.user_id == user_uuid)
                .order_by(ProviderCredential.provider_kind)
            ).all()
            return [self._record(row) for row in rows]

    def save_settings(
        self,
        principal: Principal,
        provider_kind: str,
        *,
        base_url: str,
        model_name: str,
        wire_api: str,
        api_key: str | None,
        enabled: bool,
    ) -> ProviderSettingsRecord:
        principal.require(Permission.PROVIDER_MANAGE)
        kind = self._kind(provider_kind)
        normalized_wire_api = str(wire_api or "").strip().casefold()
        if normalized_wire_api not in WIRE_APIS[kind]:
            allowed = ", ".join(sorted(value for value in WIRE_APIS[kind] if value)) or "blank"
            raise ProviderSettingsError(f"Unsupported wire API for {kind.value}; expected {allowed}.")
        normalized_base_url = validate_provider_base_url(
            effective_provider_base_url(kind, base_url),
            allow_private_urls=self.allow_private_urls,
            allowed_hosts=self.allowed_hosts,
        )

        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.user_id == user_uuid,
                    ProviderCredential.provider_kind == kind.value,
                )
            )
            submitted_key = None if api_key is None else str(api_key).strip()
            if row is None and not submitted_key:
                raise ProviderSettingsError("An API key is required when creating provider settings.")
            if row is None:
                row = ProviderCredential(
                    user_id=user_uuid,
                    provider_kind=kind.value,
                    encrypted_secret=b"",
                    encryption_key_version=self.cipher.VERSION,
                )
                session.add(row)
            if submitted_key:
                row.encrypted_secret = self.cipher.encrypt(
                    principal.user_id, kind.value, submitted_key
                )
                row.secret_hint = _secret_hint(submitted_key)
                row.encryption_key_version = self.cipher.VERSION
            row.base_url = normalized_base_url
            row.model_name = str(model_name or "").strip()
            row.wire_api = normalized_wire_api
            row.enabled = bool(enabled)
            session.flush()
            return self._record(row)

    def delete_settings(self, principal: Principal, provider_kind: str) -> bool:
        principal.require(Permission.PROVIDER_MANAGE)
        kind = self._kind(provider_kind)
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.session_factory) as session:
            result = session.execute(
                delete(ProviderCredential).where(
                    ProviderCredential.user_id == user_uuid,
                    ProviderCredential.provider_kind == kind.value,
                )
            )
            return bool(result.rowcount)

    def reveal_for_worker(self, user_id: str, provider_kind: str) -> str:
        """Internal worker boundary; never expose this value through a response schema."""
        kind = self._kind(provider_kind)
        user_uuid = uuid.UUID(user_id)
        with database_session(self.session_factory) as session:
            row = session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.user_id == user_uuid,
                    ProviderCredential.provider_kind == kind.value,
                    ProviderCredential.enabled.is_(True),
                )
            )
            if row is None:
                raise ProviderSettingsError(f"No enabled {kind.value} provider is configured.")
            return self.cipher.decrypt(user_id, kind.value, row.encrypted_secret)

    def runtime_environment(
        self,
        principal: Principal,
        *,
        provider_kinds: Iterable[ProviderKind | str] | None = None,
    ) -> dict[str, str]:
        """Build the workflow environment without persisting plaintext secrets."""

        principal.require(Permission.PROVIDER_MANAGE)
        user_uuid = uuid.UUID(principal.user_id)
        selected_kinds = (
            {self._kind(kind).value for kind in provider_kinds}
            if provider_kinds is not None
            else None
        )
        with database_session(self.session_factory) as session:
            query = select(ProviderCredential).where(
                ProviderCredential.user_id == user_uuid,
                ProviderCredential.enabled.is_(True),
            )
            if selected_kinds is not None:
                query = query.where(
                    ProviderCredential.provider_kind.in_(sorted(selected_kinds))
                )
            rows = session.scalars(query).all()

        environment: dict[str, str] = {}
        for row in rows:
            # Re-resolve immediately before handing a provider URL to a worker.
            # This closes saved-DNS drift and fails closed on rebinding to a
            # loopback/private destination.
            effective_base_url = validate_provider_base_url(
                effective_provider_base_url(row.provider_kind, row.base_url),
                allow_private_urls=self.allow_private_urls,
                allowed_hosts=self.allowed_hosts,
            )
            secret = self.cipher.decrypt(principal.user_id, row.provider_kind, row.encrypted_secret)
            if row.provider_kind == ProviderKind.MINERU.value:
                environment["MINERU_API_TOKEN"] = secret
            elif row.provider_kind == ProviderKind.TEXT.value:
                environment.update(
                    {
                        "OPENAI_API_KEY": secret,
                        "REVIEW_WRITING_API_KEY": secret,
                        "REVIEW_CONCLUSION_API_KEY": secret,
                    }
                )
                environment["OPENAI_BASE_URL"] = effective_base_url
                environment["REVIEW_WRITING_BASE_URL"] = effective_base_url
                if row.model_name:
                    environment["REVIEW_WRITING_MODEL"] = row.model_name
                    environment["REVIEW_CONCLUSION_MODEL"] = row.model_name
                if row.wire_api:
                    environment["REVIEW_WRITING_WIRE_API"] = row.wire_api
                    environment["REVIEW_CONCLUSION_WIRE_API"] = row.wire_api
            elif row.provider_kind == ProviderKind.IMAGE.value:
                environment["IMAGE_OPENAI_API_KEY"] = secret
                environment["IMAGE_OPENAI_BASE_URL"] = effective_base_url
                if row.model_name:
                    environment["IMAGE_OPENAI_MODEL"] = row.model_name
                    environment["IMAGE_FALLBACK_MODEL"] = row.model_name
                if row.wire_api:
                    environment["IMAGE_OPENAI_WIRE_API"] = row.wire_api
        return environment

    def mineru_environment(self, principal: Principal) -> dict[str, str]:
        """Return only the credential required by precise PDF parsing."""

        return self.runtime_environment(
            principal,
            provider_kinds=(ProviderKind.MINERU,),
        )
