"""Resolve and load one shared, configurable metadata/retrieval taxonomy."""

from __future__ import annotations

import ast
import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable


DEFAULT_TAXONOMY_PROFILE = "allene"
PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class TaxonomyConfigurationError(ValueError):
    """Raised when the configured taxonomy cannot be resolved or parsed."""


def resolve_taxonomy_path(
    review_root: Path,
    *,
    profile: str = "",
    rules_path: str | Path = "",
) -> Path:
    """Resolve an explicit rules file or a built-in profile.

    ``REVIEW_CLASSIFICATION_RULES`` may point to an absolute file or to a path
    relative to the workspace root. ``REVIEW_TAXONOMY_PROFILE`` selects a
    built-in profile and defaults to ``allene``.
    """
    root = Path(review_root).resolve()
    configured_path = str(rules_path or os.environ.get("REVIEW_CLASSIFICATION_RULES", "")).strip()
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise TaxonomyConfigurationError(
                f"Configured taxonomy rules file does not exist: {candidate}"
            )
        return candidate

    profile_name = str(
        profile or os.environ.get("REVIEW_TAXONOMY_PROFILE", DEFAULT_TAXONOMY_PROFILE)
    ).strip().lower()
    if not PROFILE_NAME_RE.fullmatch(profile_name):
        raise TaxonomyConfigurationError(
            "REVIEW_TAXONOMY_PROFILE must contain only lowercase letters, numbers, underscores, or hyphens."
        )
    candidate = Path(__file__).resolve().parent / "taxonomies" / f"{profile_name}.py"
    if not candidate.is_file():
        raise TaxonomyConfigurationError(
            f"Unknown taxonomy profile {profile_name!r}; expected {candidate}"
        )
    return candidate


@lru_cache(maxsize=32)
def _load_rules_cached(path_text: str, modified_ns: int) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    del modified_ns
    path = Path(path_text)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rules_node = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "rules" for target in node.targets)
        ),
        None,
    )
    if rules_node is None:
        raise TaxonomyConfigurationError(f"Taxonomy file does not define a top-level rules list: {path}")
    try:
        raw_rules = ast.literal_eval(rules_node)
    except (SyntaxError, ValueError) as exc:
        raise TaxonomyConfigurationError(f"Taxonomy rules are not literal data: {path}") from exc
    normalized: list[tuple[str, str, tuple[str, ...]]] = []
    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            raise TaxonomyConfigurationError(f"Invalid taxonomy rule #{index} in {path}")
        label = str(item[0]).strip()
        category = str(item[1]).strip()
        raw_aliases = item[2]
        if not label or not category or not isinstance(raw_aliases, (list, tuple)):
            raise TaxonomyConfigurationError(f"Invalid taxonomy rule #{index} in {path}")
        aliases = tuple(str(alias).strip() for alias in raw_aliases if str(alias).strip())
        normalized.append((label, category, aliases))
    return tuple(normalized)


def load_rules_from_path(path: Path) -> list[tuple[str, str, list[str]]]:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise TaxonomyConfigurationError(f"Taxonomy rules file does not exist: {resolved}")
    rules = _load_rules_cached(str(resolved), resolved.stat().st_mtime_ns)
    return [(label, category, list(aliases)) for label, category, aliases in rules]


def load_taxonomy_rules(review_root: Path) -> list[tuple[str, str, list[str]]]:
    return load_rules_from_path(resolve_taxonomy_path(review_root))


def labels_by_category(
    rules: Iterable[tuple[str, str, list[str]]],
    categories: Iterable[str],
) -> dict[str, list[str]]:
    result = {str(category): ["not specified"] for category in categories}
    for label, category, _aliases in rules:
        if category in result and label not in result[category]:
            result[category].append(label)
    return result


def aliases_by_category(
    rules: Iterable[tuple[str, str, list[str]]],
    categories: Iterable[str],
) -> dict[str, dict[str, list[str]]]:
    result = {str(category): {} for category in categories}
    for label, category, aliases in rules:
        if category in result and label:
            result[category][label] = list(aliases)
    return result


def taxonomy_identity(review_root: Path) -> dict[str, str]:
    path = resolve_taxonomy_path(review_root)
    raw = path.read_bytes()
    configured_path = os.environ.get("REVIEW_CLASSIFICATION_RULES", "").strip()
    try:
        relative_path = str(path.relative_to(Path(review_root).resolve()))
    except ValueError:
        relative_path = str(path)
    return {
        "profile": "custom"
        if configured_path
        else (
            os.environ.get("REVIEW_TAXONOMY_PROFILE", DEFAULT_TAXONOMY_PROFILE).strip()
            or DEFAULT_TAXONOMY_PROFILE
        ),
        "rules_path": relative_path,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
