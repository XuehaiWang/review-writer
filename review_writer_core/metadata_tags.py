"""Shared trust policy for reusable Library metadata Tags."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STRUCTURED_TAG_KEYS: tuple[str, ...] = (
    "product",
    "substrate",
    "catalyst_or_method",
    "organometallic_partner",
    "ligand_or_chiral_source",
    "leaving_group",
    "reaction_type",
    "document_scope",
)


def structured_tags_are_verified(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether the complete reusable Tag field was human-verified."""

    if not isinstance(metadata, Mapping):
        return False
    field = metadata.get("structured_tags")
    return isinstance(field, Mapping) and field.get("human_checked") is True


def verified_structured_tags(
    metadata: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return meaningful reusable Tags only after explicit human verification.

    Unmigrated rule- or LLM-generated values must not influence retrieval or
    planning.  The administrative cleanup command converts active unverified
    values to the project-neutral shape while preserving verified Tags.
    """

    if not structured_tags_are_verified(metadata):
        return {}
    field = metadata.get("structured_tags")
    value = field.get("value") if isinstance(field, Mapping) else None
    if not isinstance(value, Mapping):
        return {}
    tags: dict[str, str] = {}
    for key in STRUCTURED_TAG_KEYS:
        tag = " ".join(str(value.get(key) or "").split()).strip()
        if tag and tag.casefold() != "not specified":
            tags[key] = tag
    return tags


def neutral_structured_tag_values() -> dict[str, str]:
    """Return the project-neutral shape persisted for newly ingested papers."""

    return {key: "not specified" for key in STRUCTURED_TAG_KEYS}
