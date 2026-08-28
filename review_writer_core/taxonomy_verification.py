"""Load domain verification terms from taxonomy resources, not core scripts."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .taxonomy import resolve_taxonomy_path, suggest_taxonomy_profile


EMPTY_PROFILE: dict[str, Any] = {
    "chemical_suffixes": [],
    "named_entities": [],
    "explicit_symbols": [],
    "soft_stereo_terms": [],
    "cross_language_terms": [],
}


def _literal_profile(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = next(
        (
            item.value
            for item in tree.body
            if isinstance(item, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "verification_profile"
                for target in item.targets
            )
        ),
        None,
    )
    if node is None:
        return {}
    value = ast.literal_eval(node)
    return value if isinstance(value, dict) else {}


def load_taxonomy_verification_profile(
    review_root: Path,
    *,
    profile: str,
    topic_text: str = "",
) -> dict[str, Any]:
    selected = str(profile or "general_academic").strip().casefold()
    profiles = [selected]
    if selected == "chemistry_general" and str(topic_text or "").strip():
        specialized = suggest_taxonomy_profile(topic_text)
        if specialized not in {"general_academic", selected}:
            profiles.append(specialized)
    elif selected == "allene":
        profiles.insert(0, "chemistry_general")

    merged = {key: list(value) for key, value in EMPTY_PROFILE.items()}
    for active in profiles:
        raw = _literal_profile(resolve_taxonomy_path(review_root, profile=active))
        for key in merged:
            for item in raw.get(key) or []:
                normalized = tuple(item) if isinstance(item, list) else item
                if normalized not in merged[key]:
                    merged[key].append(normalized)
    return merged

