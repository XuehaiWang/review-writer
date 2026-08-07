"""Shared OpenAI-compatible provider normalization helpers."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit


TEXT_WIRE_APIS = {"responses", "chat-completions"}
IMAGE_WIRE_APIS = {"images", "chat-completions"}
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TEXT_MODEL = "gpt-5.4"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_TEXT_WIRE_API = "chat-completions"
DEFAULT_IMAGE_WIRE_API = "images"


def normalize_base_url(value: str, default: str = DEFAULT_OPENAI_BASE_URL) -> str:
    raw = str(value or default).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid provider base URL: {raw!r}")
    path = parsed.path.rstrip("/") or "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def normalize_wire_api(value: str, *, image: bool = False, default: str = "") -> str:
    aliases = {
        "chat": "chat-completions",
        "chat-completion": "chat-completions",
        "chat_completions": "chat-completions",
        "chat/completions": "chat-completions",
        "response": "responses",
        "image": "images",
        "image-edit": "images",
    }
    normalized = aliases.get(str(value or default).strip().lower(), str(value or default).strip().lower())
    allowed = IMAGE_WIRE_APIS if image else TEXT_WIRE_APIS
    if normalized not in allowed:
        kind = "image" if image else "text"
        raise ValueError(f"Unsupported {kind} provider wire API: {normalized!r}")
    return normalized


def openai_endpoint(base_url: str, resource: str) -> str:
    """Append an API resource exactly once, preserving an existing /v1."""
    base = normalize_base_url(base_url)
    resource = str(resource or "").strip().lstrip("/")
    if not resource:
        return base
    suffix = "/" + resource
    if base.lower().endswith(suffix.lower()):
        return base
    return base + suffix


def resolve_api_key(
    explicit: str = "",
    *,
    env_names: tuple[str, ...] = ("OPENAI_API_KEY",),
    dotenv: dict[str, str] | None = None,
) -> str:
    if str(explicit or "").strip():
        return str(explicit).strip()
    dotenv = dotenv or {}
    for name in env_names:
        value = os.environ.get(name) or dotenv.get(name)
        if str(value or "").strip():
            return str(value).strip()
    return ""
