"""Helpers for metadata fields that may carry provenance-wrapped values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def unwrap_metadata_value(value: Any) -> Any:
    """Return the value inside ``{"value": ...}`` metadata when present."""

    return value.get("value") if isinstance(value, Mapping) and "value" in value else value


def metadata_value(row: Mapping[str, Any], field: str) -> Any:
    """Read one raw or provenance-wrapped field from a metadata mapping."""

    return unwrap_metadata_value(row.get(field))
