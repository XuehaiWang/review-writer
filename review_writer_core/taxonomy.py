"""Resolve and load one shared, configurable metadata/retrieval taxonomy."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any, Iterable


DEFAULT_TAXONOMY_PROFILE = "general_academic"
# Creation preference only; keep legacy/missing-profile inference domain-neutral.
NEW_PROJECT_TAXONOMY_PROFILE = "chemistry_general"
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

# Order matters: suggest_taxonomy_profile returns the first profile whose
# signals match, so the most specific profile (allene) must precede the
# broad chemistry_general bucket.
PROFILE_TOPIC_SIGNALS: dict[str, tuple[str, ...]] = {
    "allene": (
        "allene",
        "allenation",
        "联烯",
    ),
    "chemistry_general": (
        "enantioselective",
        "diastereoselective",
        "stereoselective",
        "asymmetric catalysis",
        "asymmetric synthesis",
        "asymmetric induction",
        "cross-coupling",
        "cross coupling",
        "catalyzed",
        "catalysed",
        "catalytic",
        "catalysis",
        "organocatalytic",
        "organocatalysis",
        "organometallic",
        "photocatalytic",
        "photocatalysis",
        "photoredox",
        "electrocatalytic",
        "hydrogenation",
        "hydroboration",
        "hydrosilylation",
        "hydroamination",
        "cycloaddition",
        "metathesis",
        "olefination",
        "annulation",
        "borylation",
        "arylation",
        "silylation",
        "carbonylation",
        "c-h activation",
        "c–h activation",
        "total synthesis",
        "suzuki",
        "sonogashira",
        "negishi",
        "heterocycl",
        "atropisomer",
        "organic chemistry",
        "medicinal chemistry",
        "synthetic chemistry",
        "synthetic methodology",
    ),
}


def _active_taxonomy_profiles(profile: str, topic_text: str = "") -> list[str]:
    selected = str(profile or DEFAULT_TAXONOMY_PROFILE).strip().lower()
    # A specialist profile is an overlay on the general chemistry vocabulary,
    # not an independent replacement taxonomy.
    active = ["chemistry_general", selected] if selected == "allene" else [selected]
    if selected == "chemistry_general" and str(topic_text or "").strip():
        specialized = suggest_taxonomy_profile(topic_text)
        if specialized not in {DEFAULT_TAXONOMY_PROFILE, selected}:
            active.append(specialized)
    return active


def effective_taxonomy_profile(profile: str, topic_text: str = "") -> str:
    """Return the final profile selected by the confirmed Topic."""

    return _active_taxonomy_profiles(profile, topic_text)[-1]


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


@lru_cache(maxsize=32)
def _load_literal_cached(path_text: str, modified_ns: int, name: str) -> Any:
    del modified_ns
    path = Path(path_text)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    value_node = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ),
        None,
    )
    if value_node is None:
        raise TaxonomyConfigurationError(f"Taxonomy file does not define {name}: {path}")
    try:
        return ast.literal_eval(value_node)
    except (SyntaxError, ValueError) as exc:
        raise TaxonomyConfigurationError(f"Taxonomy extension {name} is not literal data: {path}") from exc


def load_discovery_normalization(
    review_root: Path,
    *,
    profile: str,
    topic_text: str,
) -> dict[str, Any]:
    """Load optional topic-specific Discovery normalization with a safe fallback."""

    selected = str(profile or DEFAULT_TAXONOMY_PROFILE).strip().lower()
    active = _active_taxonomy_profiles(selected, topic_text)
    specialized = next(
        (item for item in active if item not in {DEFAULT_TAXONOMY_PROFILE, "chemistry_general"}),
        "",
    )
    if not specialized:
        return {
            "status": "not_applicable",
            "profile": "",
            "config": {},
            "sha256": hashlib.sha256(b"").hexdigest(),
            "warnings": [],
            "coverage_confirmation_required": False,
        }
    try:
        path = resolve_taxonomy_path(review_root, profile=specialized)
        payload = _load_literal_cached(
            str(path.resolve()), path.stat().st_mtime_ns, "discovery_normalization"
        )
        if not isinstance(payload, dict):
            raise TaxonomyConfigurationError(
                f"Taxonomy discovery_normalization must be an object: {path}"
            )
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (OSError, TypeError, TaxonomyConfigurationError) as exc:
        return {
            "status": "degraded",
            "profile": specialized,
            "config": {},
            "sha256": hashlib.sha256(b"").hexdigest(),
            "warnings": [
                f"Specialized Discovery normalization is unavailable ({exc}); general matching remains active."
            ],
            "coverage_confirmation_required": True,
        }
    try:
        relative_path = str(path.relative_to(Path(review_root).resolve()))
    except ValueError:
        relative_path = str(path)
    return {
        "status": "enabled",
        "profile": specialized,
        "rules_path": relative_path,
        "config": payload,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "warnings": [],
        "coverage_confirmation_required": False,
    }


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
    active_profiles = _active_taxonomy_profiles(selected_profile, topic_text)

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
    topic_text: str = "",
) -> dict[str, Any]:
    root = Path(review_root).resolve()
    configured_path = str(
        rules_path or os.environ.get("REVIEW_CLASSIFICATION_RULES", "")
    ).strip()
    identity_profile = (
        "custom"
        if configured_path
        else (
            str(
                profile
                or os.environ.get(
                    "REVIEW_TAXONOMY_PROFILE", DEFAULT_TAXONOMY_PROFILE
                )
            ).strip().lower()
            or DEFAULT_TAXONOMY_PROFILE
        )
    )
    active_profiles = (
        ["custom"]
        if configured_path
        else _active_taxonomy_profiles(identity_profile, topic_text)
    )
    rules: list[dict[str, str]] = []
    digest = hashlib.sha256()
    for active_profile in active_profiles:
        path = resolve_taxonomy_path(
            root,
            profile="" if active_profile == "custom" else active_profile,
            rules_path=configured_path if active_profile == "custom" else "",
        )
        raw = path.read_bytes()
        try:
            relative_path = str(path.relative_to(root))
        except ValueError:
            relative_path = str(path)
        sha256 = hashlib.sha256(raw).hexdigest()
        rules.append(
            {"profile": active_profile, "rules_path": relative_path, "sha256": sha256}
        )
        digest.update(active_profile.encode("utf-8"))
        digest.update(relative_path.encode("utf-8"))
        digest.update(raw)

    return {
        "profile": identity_profile,
        "effective_profiles": active_profiles,
        "rules_path": rules[0]["rules_path"],
        "rules": rules,
        "sha256": digest.hexdigest(),
        "domain_rules_enabled": (
            bool(
                load_taxonomy_rules(
                    root,
                    profile=identity_profile,
                    rules_path=configured_path,
                )
            )
            if identity_profile == "custom"
            else any(taxonomy_profile_uses_domain_rules(item) for item in active_profiles)
        ),
    }
