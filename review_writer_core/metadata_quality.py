"""Shared quality flags for parsed and repaired Library metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from review_writer_core.metadata_tags import structured_tags_are_verified


def _value(metadata: dict[str, Any], key: str) -> Any:
    field = metadata.get(key)
    return field.get("value") if isinstance(field, dict) else field


def _present(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def missing_library_content_fields(
    metadata: dict[str, Any], *, has_parsed_content: bool
) -> list[str]:
    """Report absent paper content, independently of review and confidence flags.

    DOI and specialized tags are useful but not universal publication fields.
    A low extraction confidence is a review hint, not missing content.
    """
    required = ("title", "authors", "year", "journal", "abstract")
    def has_content(key: str) -> bool:
        value = _value(metadata, key)
        if isinstance(value, (list, tuple)):
            return any(_present(item) for item in value)
        return _present(value)

    missing = [key for key in required if not has_content(key)]
    if not has_parsed_content:
        missing.append("fulltext")
    return missing


def update_quality(metadata: dict[str, Any]) -> None:
    """Recompute derived flags, preserving unrelated diagnostic warnings."""

    prior = metadata.get("quality") or {}
    generated = {
        "empty_authors", "missing_journal", "missing_doi",
        "low_confidence_title", "low_confidence_abstract",
    }
    warnings = [
        str(item) for item in prior.get("warnings", [])
        if str(item) not in generated and not str(item).startswith("structured_tag_not_specified_")
    ]
    missing = [key for key in ("title", "abstract", "year") if not _present(_value(metadata, key))]
    if not _present(_value(metadata, "authors")):
        warnings.append("empty_authors")
    for key in ("journal", "doi"):
        if not _present(_value(metadata, key)):
            warnings.append(f"missing_{key}")
    verified_tags = structured_tags_are_verified(metadata)
    if verified_tags:
        tag_values = _value(metadata, "structured_tags")
        for key, value in (tag_values if isinstance(tag_values, Mapping) else {}).items():
            if not value or str(value).casefold() == "not specified":
                warnings.append(f"structured_tag_not_specified_{key}")
    confidence_keys = ("title", "authors", "year", "journal", "doi", "abstract")
    if verified_tags:
        confidence_keys += ("structured_tags",)
    confidence = [
        float(metadata[key].get("confidence") or 0)
        for key in confidence_keys if isinstance(metadata.get(key), dict)
    ]
    for key in ("title", "abstract"):
        field = metadata.get(key) or {}
        if isinstance(field, dict) and float(field.get("confidence") or 0) < 0.75:
            warnings.append(f"low_confidence_{key}")
    warnings = list(dict.fromkeys(warnings))
    metadata["quality"] = {
        "missing_fields": missing,
        "warnings": warnings,
        "overall_confidence": round(sum(confidence) / len(confidence), 3) if confidence else 0,
        "needs_human_check": bool(
            missing or warnings or (metadata.get("human_review") or {}).get("status") != "reviewed"
        ),
    }
