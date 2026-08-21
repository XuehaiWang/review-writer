"""Resolve and load one shared, configurable metadata/retrieval taxonomy."""

from __future__ import annotations

import ast
import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any, Iterable


DEFAULT_TAXONOMY_PROFILE = "general_academic"
PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


@dataclass(frozen=True)
class TaxonomyProfileDefinition:
    id: str
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    domain_rules_enabled: bool


TAXONOMY_PROFILES: tuple[TaxonomyProfileDefinition, ...] = (
    TaxonomyProfileDefinition(
        id="general_academic",
        label_zh="通用学术",
        label_en="General Academic",
        description_zh="使用通用查询与全文召回，不启用化学领域扩展或标签加权。",
        description_en=(
            "Uses general query planning and full-text recall without chemistry "
            "expansion or tag weighting."
        ),
        domain_rules_enabled=False,
    ),
    TaxonomyProfileDefinition(
        id="chemistry_general",
        label_zh="通用化学",
        label_en="General Chemistry",
        description_zh="在通用召回基础上增加化学别名扩展和结构化标签加权。",
        description_en=(
            "Adds chemistry alias expansion and structured-tag weighting on top "
            "of general recall."
        ),
        domain_rules_enabled=True,
    ),
    TaxonomyProfileDefinition(
        id="allene",
        label_zh="联烯化学",
        label_en="Allene Chemistry",
        description_zh="为联烯、轴手性和相关合成主题启用专用化学规则。",
        description_en=(
            "Enables specialized chemistry rules for allenes, axial chirality, "
            "and related synthesis topics."
        ),
        domain_rules_enabled=True,
    ),
)
TAXONOMY_PROFILE_BY_ID = {item.id: item for item in TAXONOMY_PROFILES}
PUBLIC_TAXONOMY_PROFILE_IDS = frozenset({"general_academic", "chemistry_general"})

PROFILE_TOPIC_SIGNALS: dict[str, tuple[str, ...]] = {
    "allene": (
        "allene",
        "allenation",
        "propargylic",
        "propargyl",
        "allenylidene",
        "axial chirality",
        "sn2'",
    ),
}


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
    built-in profile and defaults to the no-domain-rules ``general_academic``
    profile.
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


def load_taxonomy_rules(
    review_root: Path,
    *,
    profile: str = "",
    rules_path: str | Path = "",
    topic_text: str = "",
) -> list[tuple[str, str, list[str]]]:
    configured_rules_path = str(
        rules_path or os.environ.get("REVIEW_CLASSIFICATION_RULES", "")
    ).strip()
    if configured_rules_path:
        return load_rules_from_path(
            resolve_taxonomy_path(
                review_root,
                profile=profile,
                rules_path=configured_rules_path,
            )
        )

    selected_profile = str(
        profile
        or os.environ.get("REVIEW_TAXONOMY_PROFILE", DEFAULT_TAXONOMY_PROFILE)
    ).strip().lower()
    active_profiles = [selected_profile]
    if selected_profile == "chemistry_general" and str(topic_text or "").strip():
        specialized = suggest_taxonomy_profile(topic_text)
        if specialized not in {DEFAULT_TAXONOMY_PROFILE, selected_profile}:
            active_profiles.append(specialized)

    combined: list[tuple[str, str, list[str]]] = []
    indexes: dict[tuple[str, str], int] = {}
    for active_profile in active_profiles:
        path = resolve_taxonomy_path(review_root, profile=active_profile)
        for label, category, aliases in load_rules_from_path(path):
            key = (category, label)
            if key not in indexes:
                indexes[key] = len(combined)
                combined.append((label, category, list(aliases)))
                continue
            current = combined[indexes[key]][2]
            current.extend(alias for alias in aliases if alias not in current)
    return combined


def load_validation_taxonomy_rules(
    review_root: Path,
) -> list[tuple[str, str, list[str]]]:
    """Load labels accepted by the shared library metadata validator.

    A library may contain papers from projects using different profiles.  An
    explicit operator override remains strict; otherwise validation accepts
    the union of installed built-in profiles while retrieval stays bound to
    one project profile.
    """
    if (
        os.environ.get("REVIEW_CLASSIFICATION_RULES", "").strip()
        or os.environ.get("REVIEW_TAXONOMY_PROFILE", "").strip()
    ):
        return load_taxonomy_rules(review_root)
    combined: list[tuple[str, str, list[str]]] = []
    seen: set[tuple[str, str]] = set()
    profiles_dir = Path(__file__).resolve().parent / "taxonomies"
    for path in sorted(profiles_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for label, category, aliases in load_rules_from_path(path):
            key = (category, label)
            if key not in seen:
                seen.add(key)
                combined.append((label, category, aliases))
    return combined


def suggest_taxonomy_profile(topic: str) -> str:
    """Select a built-in profile from explicit topic signals.

    Topic-specific profiles opt in only on a positive match; unrelated topics
    therefore never inherit the allene vocabulary by accident.
    """
    normalized = re.sub(r"\s+", " ", str(topic or "").strip().casefold())
    for profile, signals in PROFILE_TOPIC_SIGNALS.items():
        if any(signal.casefold() in normalized for signal in signals):
            return profile
    return DEFAULT_TAXONOMY_PROFILE


def taxonomy_profile_catalog() -> list[dict[str, Any]]:
    """Return stable public metadata for user-selectable profiles.

    Topic-specific profiles such as ``allene`` remain installed so existing
    projects and internal topic routing keep their specialist vocabulary, but
    they are not exposed as top-level project categories.
    """

    return [
        asdict(item)
        for item in TAXONOMY_PROFILES
        if item.id in PUBLIC_TAXONOMY_PROFILE_IDS
    ]


def validate_taxonomy_profile(profile: str) -> str:
    """Validate a project-selected built-in profile and return its stable ID."""

    normalized = str(profile or "").strip().lower()
    if normalized not in TAXONOMY_PROFILE_BY_ID:
        raise TaxonomyConfigurationError(f"Unknown taxonomy profile: {normalized or '<empty>'}")
    return normalized


def validate_selectable_taxonomy_profile(profile: str) -> str:
    """Validate a taxonomy profile accepted from the public project UI/API."""

    normalized = validate_taxonomy_profile(profile)
    if normalized not in PUBLIC_TAXONOMY_PROFILE_IDS:
        raise TaxonomyConfigurationError(
            f"Taxonomy profile is internal and cannot be selected: {normalized}"
        )
    return normalized


def taxonomy_profile_uses_domain_rules(profile: str) -> bool:
    normalized = validate_taxonomy_profile(profile)
    return TAXONOMY_PROFILE_BY_ID[normalized].domain_rules_enabled


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


def taxonomy_identity(
    review_root: Path,
    *,
    profile: str = "",
    rules_path: str | Path = "",
) -> dict[str, Any]:
    path = resolve_taxonomy_path(review_root, profile=profile, rules_path=rules_path)
    raw = path.read_bytes()
    configured_path = os.environ.get("REVIEW_CLASSIFICATION_RULES", "").strip()
    try:
        relative_path = str(path.relative_to(Path(review_root).resolve()))
    except ValueError:
        relative_path = str(path)
    identity_profile = (
        "custom"
        if configured_path
        else (
            str(
                profile
                or os.environ.get(
                    "REVIEW_TAXONOMY_PROFILE", DEFAULT_TAXONOMY_PROFILE
                )
            ).strip()
            or DEFAULT_TAXONOMY_PROFILE
        )
    )
    return {
        "profile": identity_profile,
        "rules_path": relative_path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "domain_rules_enabled": (
            bool(load_rules_from_path(path))
            if identity_profile == "custom"
            else taxonomy_profile_uses_domain_rules(identity_profile)
        ),
    }
