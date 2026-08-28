"""Project-level topic and taxonomy configuration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .taxonomy import DEFAULT_TAXONOMY_PROFILE, validate_taxonomy_profile
from .workspace import WorkspacePaths


PROJECT_CONFIG_NAME = "project_config.json"
PROJECT_CONFIG_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def project_config_path(review_root: Path, project_id: str) -> Path:
    return WorkspacePaths(Path(review_root).resolve()).project(project_id) / PROJECT_CONFIG_NAME


def load_project_config(review_root: Path, project_id: str) -> dict[str, Any]:
    path = project_config_path(review_root, project_id)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_project_config(
    review_root: Path,
    project_id: str,
    *,
    topic: str = "",
    taxonomy_profile: str = "",
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update project configuration without discarding unknown keys."""
    path = project_config_path(review_root, project_id)
    current = load_project_config(review_root, project_id)
    resolved_topic = str(topic or current.get("topic") or "").strip()
    resolved_profile = str(
        taxonomy_profile
        or current.get("taxonomy_profile")
        or DEFAULT_TAXONOMY_PROFILE
    ).strip()
    resolved_profile = validate_taxonomy_profile(resolved_profile)
    current.update(
        {
            "schema_version": PROJECT_CONFIG_SCHEMA_VERSION,
            "project_id": project_id,
            "topic": resolved_topic,
            "taxonomy_profile": resolved_profile,
            "updated_at": utc_now(),
        }
    )
    if updates:
        current.update(updates)
    atomic_write_json(path, current)
    return current


def project_taxonomy_profile(
    review_root: Path,
    project_id: str,
    *,
    topic: str = "",
) -> str:
    config = load_project_config(review_root, project_id)
    configured = str(config.get("taxonomy_profile") or "").strip()
    if configured:
        return configured
    return DEFAULT_TAXONOMY_PROFILE
