#!/usr/bin/env python3
"""Local provider settings for Review Writer without exposing stored secrets."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SETTINGS_VERSION = 1
SETTINGS_RELATIVE_PATH = Path(".review-writer") / "provider-settings.json"
TEXT_WIRE_APIS = {"chat-completions", "responses"}
IMAGE_WIRE_APIS = {"images", "chat-completions"}
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def settings_path(review_root: Path) -> Path:
    return Path(review_root).resolve() / SETTINGS_RELATIVE_PATH


def _read_document(review_root: Path) -> dict[str, Any]:
    path = settings_path(review_root)
    if not path.is_file():
        return {"version": SETTINGS_VERSION, "values": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider settings are unreadable: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
        raise RuntimeError(f"Provider settings have an invalid structure: {path}")
    return data


def _read_dotenv_values(review_root: Path) -> dict[str, str]:
    """Read a workspace .env without mutating the dashboard process."""
    path = Path(review_root).resolve() / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip("'\"")
    return values


def _write_document(review_root: Path, values: dict[str, str]) -> Path:
    path = settings_path(review_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SETTINGS_VERSION,
        "updated_at": utc_now(),
        "values": dict(sorted(values.items())),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _clean_string(value: Any, field: str, max_length: int = 2048) -> str:
    text = str(value or "").strip()
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{field} contains control characters.")
    if len(text) > max_length:
        raise ValueError(f"{field} is too long.")
    return text


def _validate_url(value: Any, field: str) -> str:
    text = _clean_string(value, field).rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{field} must not contain credentials.")
    return text


def _validate_key(value: Any, field: str) -> str:
    text = _clean_string(value, field, 4096)
    if text and len(text) < 8:
        raise ValueError(f"{field} is too short.")
    return text


def _validate_model(value: Any, field: str) -> str:
    text = _clean_string(value, field, 128)
    if text and not _MODEL_RE.fullmatch(text):
        raise ValueError(f"{field} contains unsupported characters.")
    return text


def _validate_wire(value: Any, field: str, allowed: set[str]) -> str:
    text = _clean_string(value, field, 64).casefold().replace("_", "-")
    if text and text not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}.")
    return text


def _effective(values: dict[str, str], key: str, default: str = "") -> str:
    if key in values:
        return str(values.get(key) or "").strip()
    return str(os.environ.get(key, default) or "").strip()


def _source(values: dict[str, str], keys: tuple[str, ...]) -> str:
    if any(str(values.get(key) or "").strip() for key in keys):
        return "local-settings"
    if any(str(os.environ.get(key) or "").strip() for key in keys):
        return "environment"
    return "unset"


def _masked(value: str) -> str:
    if not value:
        return ""
    tail = value[-4:] if len(value) >= 4 else value[-1:]
    return "********" + tail


def _legacy_mineru_key(review_root: Path) -> str:
    path = (
        Path(review_root).resolve()
        / "skills"
        / "mineru-precise-parse-review-writer"
        / "config"
        / "mineru_api_token.txt"
    )
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def apply_saved_provider_settings(review_root: Path) -> dict[str, str]:
    """Apply local settings to the current process so all future subprocesses inherit them."""
    values = {
        str(key): str(value)
        for key, value in (_read_document(review_root).get("values") or {}).items()
        if str(key).strip() and isinstance(value, str)
    }
    for key, value in values.items():
        os.environ[key] = value
    return values


def provider_subprocess_environment(review_root: Path) -> dict[str, str]:
    """Build a fresh environment for every workflow subprocess.

    Workspace .env values override stale inherited variables, while settings
    saved through the UI take final precedence. This keeps later stages tied to
    the workspace shown in Settings instead of to whichever shell launched the
    dashboard.
    """
    root = Path(review_root).resolve()
    environment = dict(os.environ)
    environment.update(_read_dotenv_values(root))
    saved = {
        str(key): str(value)
        for key, value in (_read_document(root).get("values") or {}).items()
        if str(key).strip() and isinstance(value, str)
    }
    environment.update(saved)
    if not str(environment.get("MINERU_API_TOKEN") or "").strip():
        legacy_key = _legacy_mineru_key(root)
        if legacy_key:
            environment["MINERU_API_TOKEN"] = legacy_key
    return environment


def public_provider_settings(review_root: Path) -> dict[str, Any]:
    root = Path(review_root).resolve()
    path = settings_path(root)
    values = apply_saved_provider_settings(root)
    mineru_key = _effective(values, "MINERU_API_TOKEN")
    mineru_source = _source(values, ("MINERU_API_TOKEN",))
    if not mineru_key:
        mineru_key = _legacy_mineru_key(review_root)
        if mineru_key:
            mineru_source = "legacy-token-file"
    text_key = _effective(values, "REVIEW_WRITING_API_KEY") or _effective(values, "OPENAI_API_KEY")
    image_key = _effective(values, "IMAGE_OPENAI_API_KEY")
    return {
        "ok": True,
        "storage": {
            "scope": "local-workspace",
            "git_ignored": True,
            "workspace_root": str(root),
            "settings_path": str(path),
            "settings_file_exists": path.is_file(),
            "updated_at": _read_document(root).get("updated_at"),
        },
        "mineru": {
            "key_configured": bool(mineru_key),
            "key_hint": _masked(mineru_key),
            "source": mineru_source,
        },
        "text": {
            "base_url": _effective(values, "REVIEW_WRITING_BASE_URL") or _effective(values, "OPENAI_BASE_URL"),
            "model": _effective(values, "REVIEW_WRITING_MODEL", "gpt-5.4"),
            "wire_api": _effective(values, "REVIEW_WRITING_WIRE_API", "chat-completions"),
            "key_configured": bool(text_key),
            "key_hint": _masked(text_key),
            "source": _source(values, ("REVIEW_WRITING_API_KEY", "OPENAI_API_KEY")),
        },
        "image": {
            "base_url": _effective(values, "IMAGE_OPENAI_BASE_URL") or _effective(values, "OPENAI_BASE_URL"),
            "model": _effective(values, "IMAGE_OPENAI_MODEL", "gpt-image-2"),
            "wire_api": _effective(values, "IMAGE_OPENAI_WIRE_API", "images"),
            "key_configured": bool(image_key),
            "key_hint": _masked(image_key),
            "source": _source(values, ("IMAGE_OPENAI_API_KEY",)),
        },
    }


def save_provider_settings(review_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Settings payload must be a JSON object.")
    mineru = payload.get("mineru") or {}
    text = payload.get("text") or {}
    image = payload.get("image") or {}
    if not all(isinstance(section, dict) for section in (mineru, text, image)):
        raise ValueError("MinerU, text, and image settings must be JSON objects.")

    document = _read_document(review_root)
    values = {
        str(key): str(value)
        for key, value in (document.get("values") or {}).items()
        if isinstance(value, str)
    }

    text_url = _validate_url(text.get("base_url"), "Text API URL")
    text_model = _validate_model(text.get("model"), "Text model")
    text_wire = _validate_wire(text.get("wire_api"), "Text wire API", TEXT_WIRE_APIS)
    image_url = _validate_url(image.get("base_url"), "Image API URL")
    image_model = _validate_model(image.get("model"), "Image model")
    image_wire = _validate_wire(image.get("wire_api"), "Image wire API", IMAGE_WIRE_APIS)

    if text_url:
        values["OPENAI_BASE_URL"] = text_url
        values["REVIEW_WRITING_BASE_URL"] = text_url
    if text_model:
        values["REVIEW_WRITING_MODEL"] = text_model
        values["REVIEW_CONCLUSION_MODEL"] = text_model
    if text_wire:
        values["REVIEW_WRITING_WIRE_API"] = text_wire
        values["REVIEW_CONCLUSION_WIRE_API"] = text_wire
    if image_url:
        values["IMAGE_OPENAI_BASE_URL"] = image_url
    if image_model:
        values["IMAGE_OPENAI_MODEL"] = image_model
        values["IMAGE_FALLBACK_MODEL"] = image_model
    if image_wire:
        values["IMAGE_OPENAI_WIRE_API"] = image_wire

    mineru_key = _validate_key(mineru.get("api_key"), "MinerU key")
    text_key = _validate_key(text.get("api_key"), "Text API key")
    image_key = _validate_key(image.get("api_key"), "Image API key")
    # Blank secret fields mean "keep the current key". Persist the effective
    # environment/.env key into the Git-ignored settings document so later
    # workflow subprocesses are not coupled to the dashboard launch shell.
    effective_environment = provider_subprocess_environment(review_root)
    if not mineru_key:
        mineru_key = str(effective_environment.get("MINERU_API_TOKEN") or "").strip()
    if not text_key:
        text_key = str(
            effective_environment.get("REVIEW_WRITING_API_KEY")
            or effective_environment.get("OPENAI_API_KEY")
            or ""
        ).strip()
    if not image_key:
        image_key = str(effective_environment.get("IMAGE_OPENAI_API_KEY") or "").strip()
    if mineru_key:
        values["MINERU_API_TOKEN"] = mineru_key
    if text_key:
        values["OPENAI_API_KEY"] = text_key
        values["REVIEW_WRITING_API_KEY"] = text_key
        values["REVIEW_CONCLUSION_API_KEY"] = text_key
    if image_key:
        values["IMAGE_OPENAI_API_KEY"] = image_key

    _write_document(review_root, values)
    apply_saved_provider_settings(review_root)
    return public_provider_settings(review_root)
