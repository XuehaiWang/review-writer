"""Validated runtime limits shared by CLI and dashboard workflows."""

from __future__ import annotations

import os


def integer_setting(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1000,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def literature_batch_limit() -> int:
    return integer_setting("REVIEW_MAX_LITERATURE_BATCH", 30, maximum=200)
