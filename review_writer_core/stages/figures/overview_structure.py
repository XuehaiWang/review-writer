"""Evidence-aware representative-structure contracts for overview figures.

The topic can mention substrates, products, catalysts, and methods in the same
sentence. A flat keyword lookup cannot safely decide which molecule represents
the review, so this module resolves the semantic role before exposing a SMILES
string to the renderer. Domain motifs live in taxonomy resources rather than
in this generic core module.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from review_writer_core.stages.figures.chemical_names import resolve_chemical_name
from review_writer_core.taxonomy import suggest_taxonomy_profile


OVERVIEW_STRUCTURE_SCHEMA_VERSION = 1
_STRUCTURE_TAXONOMY_ROOT = Path(__file__).resolve().parents[2] / "taxonomies" / "structures"
_SYNTHESIS_INTENT = re.compile(
    r"\b(?:synthes(?:is|es|ize|ized|izing)|prepar(?:e|ation)|form(?:ation|ing)?|"
    r"construct(?:ion|ing)?|access(?:ing|ed)?|afford(?:ing|ed)?|produc(?:e|tion|ing))\b",
    re.IGNORECASE,
)
_TARGET_PHRASE = re.compile(
    r"\b(?:to\s+access|access(?:ing|ed)?|to\s+afford|afford(?:ing|ed)?|"
    r"to\s+produce|produc(?:e|ing)|formation\s+of|synthes(?:is|es)\s+of|"
    r"preparation\s+of|construction\s+of)\s+([^.;:\n]{2,180})",
    re.IGNORECASE,
)
_TARGET_BOUNDARY = re.compile(
    r"\s+\b(?:from|using|via|with|under|through|by|categorized|classified|organized|focusing)\b.*$",
    re.IGNORECASE,
)
_SMILES_KEYS = ("smiles", "canonical_smiles", "isomeric_smiles")


@lru_cache(maxsize=16)
def _load_taxonomy_payload(name: str) -> dict[str, Any]:
    path = _STRUCTURE_TAXONOMY_ROOT / f"{name}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


@lru_cache(maxsize=16)
def _load_taxonomy_file(name: str) -> tuple[dict[str, Any], ...]:
    motifs = _load_taxonomy_payload(name).get("motifs")
    return tuple(dict(item) for item in motifs or [] if isinstance(item, dict) and item.get("motif"))


@lru_cache(maxsize=16)
def _motifs_for_profile(profile: str) -> tuple[dict[str, Any], ...]:
    """Load specific motifs first, followed by the general chemistry registry."""

    normalized = str(profile or "").casefold().strip()
    if normalized in {"", "general_academic"}:
        return ()
    resources: list[tuple[dict[str, Any], ...]] = []
    if normalized not in {"chemistry", "chemistry_general"}:
        resources.append(_load_taxonomy_file(normalized))
    resources.append(_load_taxonomy_file("chemistry_general"))
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource in resources:
        for item in resource:
            name = str(item.get("motif") or "").strip()
            if name and name not in seen:
                seen.add(name)
                merged.append(dict(item))
    return tuple(merged)


def _text_values(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        text = " ".join(value.split()).strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        preferred = value.get("value")
        if preferred is not None:
            yield from _text_values(preferred)
            return
        for nested in value.values():
            yield from _text_values(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _text_values(nested)


def _matrix_rows(matrix: Any) -> list[dict[str, Any]]:
    if not isinstance(matrix, dict):
        return []
    rows = matrix.get("rows")
    if not isinstance(rows, list):
        rows = matrix.get("papers")
    return [row for row in rows or [] if isinstance(row, dict)]


def _matrix_role_values(matrix: Any, role: str) -> list[str]:
    aliases = {
        "product": {"product", "product_class", "target_product", "product_type"},
        "substrate": {"substrate", "substrate_class", "starting_material", "precursor"},
    }[role]
    values: list[str] = []
    for row in _matrix_rows(matrix):
        for key in aliases:
            values.extend(_text_values(row.get(key)))
        structured = row.get("structured_tags")
        if isinstance(structured, dict):
            for key in aliases:
                values.extend(_text_values(structured.get(key)))
        for fact in row.get("scientific_facts") or []:
            if not isinstance(fact, dict):
                continue
            identity = " ".join(
                str(fact.get(key) or "")
                for key in ("fact_type", "field", "role", "label", "category")
            ).casefold()
            if any(alias.replace("_", " ") in identity.replace("_", " ") for alias in aliases):
                values.extend(_text_values(fact.get("value") or fact.get("statement")))
    return values


def _query_role_values(query_plan: Any, role: str) -> list[str]:
    if not isinstance(query_plan, dict):
        return []
    return [
        text
        for item in query_plan.get("keywords") or []
        if isinstance(item, dict) and str(item.get("category") or "").casefold() == role
        for text in _text_values(item.get("keyword") or item.get("value"))
    ]


def _trim_candidate_name(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip(" .,:;()[]{}")
    text = _TARGET_BOUNDARY.sub("", text).strip(" .,:;()[]{}")
    return text if 1 < len(text) <= 160 else ""


def _topic_target_values(topic: str) -> list[str]:
    return [
        candidate
        for match in _TARGET_PHRASE.finditer(topic or "")
        if (candidate := _trim_candidate_name(match.group(1)))
    ]


def _motif_from_text(values: Iterable[str], motifs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    texts = [str(value or "") for value in values if str(value or "").strip()]
    items = tuple(motifs)
    if not texts or not items:
        return None
    scores: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    by_name = {str(item["motif"]): item for item in items}
    for text_index, text in enumerate(texts):
        for item in items:
            name = str(item["motif"])
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in item.get("patterns") or []):
                scores[name] += 1
                first_seen.setdefault(name, text_index)
    if not scores:
        return None
    selected = min(scores, key=lambda name: (-scores[name], first_seen[name]))
    return dict(by_name[selected])


def _transformation_motif(topic: str, motifs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for item in motifs:
        if any(
            re.search(pattern, topic or "", re.IGNORECASE)
            for pattern in item.get("transformation_patterns") or []
        ):
            return dict(item)
    return None


def _smiles_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in _SMILES_KEYS:
        smiles = str(value.get(key) or "").strip()
        if smiles:
            return smiles
    return ""


def _explicit_query_structures(query_plan: Any) -> list[tuple[str, str, str]]:
    if not isinstance(query_plan, dict):
        return []
    candidates: list[tuple[str, str, str]] = []
    representative = query_plan.get("representative_structure")
    smiles = _smiles_from_mapping(representative)
    if smiles:
        role = str((representative or {}).get("role") or "target_product").casefold().strip()
        label = str((representative or {}).get("label") or (representative or {}).get("name") or "")
        candidates.append((smiles, role, label))
    top_level_smiles = str(
        query_plan.get("skeleton_smiles") or query_plan.get("smiles") or ""
    ).strip()
    if top_level_smiles:
        candidates.append((top_level_smiles, "target_product", ""))
    for item in query_plan.get("keywords") or []:
        if not isinstance(item, dict) or str(item.get("category") or "").casefold() != "product":
            continue
        smiles = _smiles_from_mapping(item)
        if smiles:
            label = str(item.get("keyword") or item.get("value") or "")
            candidates.append((smiles, "target_product", label))
    return candidates


def _explicit_matrix_product_structures(matrix: Any) -> list[tuple[str, str, str]]:
    aliases = ("product", "product_class", "target_product", "product_type")
    candidates: list[tuple[str, str, str]] = []
    for row in _matrix_rows(matrix):
        for key in ("product_smiles", "target_product_smiles"):
            smiles = str(row.get(key) or "").strip()
            if smiles:
                candidates.append((smiles, "target_product", str(row.get("product") or "")))
        structured = row.get("structured_tags")
        if isinstance(structured, dict):
            for key in aliases:
                value = structured.get(key)
                smiles = _smiles_from_mapping(value)
                if smiles:
                    label = next(iter(_text_values(value)), "")
                    candidates.append((smiles, "target_product", label))
        for fact in row.get("scientific_facts") or []:
            if not isinstance(fact, dict):
                continue
            identity = " ".join(
                str(fact.get(key) or "")
                for key in ("fact_type", "field", "role", "label", "category")
            ).casefold().replace("_", " ")
            if "product" not in identity:
                continue
            smiles = _smiles_from_mapping(fact)
            if not smiles and isinstance(fact.get("value"), dict):
                smiles = _smiles_from_mapping(fact.get("value"))
            if smiles:
                candidates.append((smiles, "target_product", str(fact.get("label") or "")))
    return candidates


def _validated_contract(contract: dict[str, Any]) -> dict[str, Any]:
    result = dict(contract)
    smiles = str(result.get("smiles") or "").strip()
    required = str(result.get("required_smarts") or "").strip()
    if not smiles:
        result["validation_status"] = "not_applicable"
        return result
    try:
        from rdkit import Chem

        molecule = Chem.MolFromSmiles(smiles)
        pattern = Chem.MolFromSmarts(required) if required else None
        valid = bool(molecule) and (pattern is None or molecule.HasSubstructMatch(pattern))
    except ImportError:
        valid = bool(smiles) and (not required or required in smiles)
    except Exception:
        valid = False
    if valid:
        result["validation_status"] = "validated"
        return result
    result.update(
        {
            "status": "unresolved",
            "smiles": "",
            "validation_status": "structure_motif_mismatch",
            "reason": "The proposed representative structure failed structural validation.",
        }
    )
    return result


def _explicit_structure_contract(
    base: dict[str, Any],
    candidates: list[tuple[str, str, str]],
    *,
    source: str,
    confidence: float,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    counts = Counter(smiles for smiles, _, _ in candidates)
    ordered = sorted(enumerate(candidates), key=lambda pair: (-counts[pair[1][0]], pair[0]))
    for _, (smiles, role, label) in ordered:
        candidate = _validated_contract(
            {
                **base,
                "status": "resolved",
                "role": role if role in {"target_product", "primary_subject"} else "target_product",
                "label": label or "representative chemical structure",
                "smiles": smiles,
                "motif": "explicit_structure",
                "confidence": confidence,
                "evidence_sources": [source],
            }
        )
        if candidate.get("status") == "resolved":
            return candidate
    return None


def derive_overview_structure_contract(
    topic: Any,
    *,
    query_plan: Any = None,
    matrix: Any = None,
    sections: Any = None,
    taxonomy_profile: Any = "",
) -> dict[str, Any]:
    """Resolve the molecule that should represent a review overview.

    Priority is explicit verified structure, Matrix product structure, semantic
    product role, then a taxonomy motif. Synthesis-like topics are product
    locked: a substrate can never become a silent fallback.
    """

    normalized_topic = " ".join(str(topic or "").split()).strip()
    profile = str(taxonomy_profile or "").casefold().strip()
    effective_profile = profile
    if profile in {"chemistry", "chemistry_general"}:
        suggested = suggest_taxonomy_profile(normalized_topic)
        if suggested == "allene":
            effective_profile = suggested
    motifs = _motifs_for_profile(effective_profile)
    profile_resource = "chemistry_general" if effective_profile == "chemistry" else effective_profile
    profile_has_structure_registry = bool(
        profile_resource and _load_taxonomy_file(profile_resource)
    )
    resource_warning = (
        "Specialized structure registry is unavailable; no taxonomy structure was guessed."
        if effective_profile not in {"", "general_academic", "chemistry", "chemistry_general"}
        and not profile_has_structure_registry
        else ""
    )
    topic_motif = _motif_from_text([normalized_topic], motifs)
    transformation_motif = _transformation_motif(normalized_topic, motifs)
    explicit_query = _explicit_query_structures(query_plan)
    explicit_matrix = _explicit_matrix_product_structures(matrix)
    chemistry_context = (
        profile_has_structure_registry
        or bool(topic_motif)
        or bool(transformation_motif)
        or bool(explicit_query)
        or bool(explicit_matrix)
    )
    base = {
        "schema_version": OVERVIEW_STRUCTURE_SCHEMA_VERSION,
        "status": "unresolved",
        "role": "none",
        "label": "",
        "smiles": "",
        "motif": "",
        "required_smarts": "",
        "confidence": 0.0,
        "evidence_sources": [],
        "candidate_names": [],
        "validation_status": "not_applicable",
        "taxonomy_profile": profile,
        "effective_taxonomy_profile": effective_profile,
        "warnings": [resource_warning] if resource_warning else [],
    }
    if not chemistry_context:
        return {**base, "status": "not_applicable", "reason": "No chemical structure is required."}

    explicit = _explicit_structure_contract(
        base, explicit_query, source="query_plan_explicit_structure", confidence=0.99
    )
    if explicit:
        return explicit
    explicit = _explicit_structure_contract(
        base, explicit_matrix, source="matrix_product_structure", confidence=0.97
    )
    if explicit:
        return explicit

    product_sources = (
        ("query_plan_product", _query_role_values(query_plan, "product"), 0.96),
        ("matrix_product_facts", _matrix_role_values(matrix, "product"), 0.92),
        ("topic_target_phrase", _topic_target_values(normalized_topic), 0.86),
    )
    candidate_names: list[str] = []
    for source, values, confidence in product_sources:
        cleaned = [candidate for value in values if (candidate := _trim_candidate_name(value))]
        candidate_names.extend(cleaned)
        motif = _motif_from_text(cleaned, motifs)
        if motif is not None:
            return _validated_contract(
                {
                    **base,
                    **motif,
                    "status": "resolved",
                    "role": "target_product",
                    "confidence": confidence,
                    "evidence_sources": [source],
                    "candidate_names": list(dict.fromkeys(candidate_names))[:6],
                }
            )

    synthesis_intent = bool(_SYNTHESIS_INTENT.search(normalized_topic) or transformation_motif)
    if transformation_motif is not None:
        return _validated_contract(
            {
                **base,
                **transformation_motif,
                "status": "resolved",
                "role": "target_product",
                "confidence": 0.80,
                "evidence_sources": ["topic_transformation_taxonomy"],
                "candidate_names": list(dict.fromkeys(candidate_names))[:6],
            }
        )
    if synthesis_intent:
        return {
            **base,
            "role": "target_product",
            "candidate_names": list(dict.fromkeys(candidate_names))[:6],
            "reason": "A synthesis target is implied, but no reliable product structure was resolved.",
            "evidence_sources": ["topic_synthesis_intent"],
        }

    subject_values = [normalized_topic]
    subject_values.extend(_query_role_values(query_plan, "substrate"))
    subject_values.extend(_matrix_role_values(matrix, "substrate"))
    if isinstance(sections, list):
        subject_values.extend(
            str(section.get("title") or section.get("heading") or "")
            for section in sections
            if isinstance(section, dict)
        )
    motif = _motif_from_text(subject_values, motifs)
    if motif is not None:
        return _validated_contract(
            {
                **base,
                **motif,
                "status": "resolved",
                "role": "primary_subject",
                "confidence": 0.72,
                "evidence_sources": ["topic_primary_subject"],
            }
        )
    return {**base, "reason": "No reliable representative chemical structure was resolved."}


def resolve_contract_chemical_name(
    contract: Any,
    *,
    resolver: Callable[..., dict[str, Any] | None] | None = None,
    timeout_seconds: float = 4.0,
) -> dict[str, Any]:
    """Resolve unresolved candidate names and validate the returned structure.

    The resolver is intentionally kept outside synchronous Blueprint creation.
    Image workers may call this helper; failures simply preserve the unresolved
    contract and never stop figure generation.
    """

    if not isinstance(contract, dict):
        return {}
    result = dict(contract)
    if result.get("status") == "resolved" or result.get("role") not in {
        "target_product",
        "primary_subject",
    }:
        return result
    resolve = resolver or resolve_chemical_name
    for candidate_name in result.get("candidate_names") or []:
        try:
            resolution = resolve(candidate_name, timeout_seconds=timeout_seconds)
        except Exception:
            resolution = None
        if not isinstance(resolution, dict) or not resolution.get("smiles"):
            continue
        resolved = _validated_contract(
            {
                **result,
                "status": "resolved",
                "label": str(resolution.get("resolved_name") or candidate_name),
                "smiles": str(resolution.get("smiles") or ""),
                "motif": "resolved_chemical_name",
                "required_smarts": "",
                "confidence": max(float(result.get("confidence") or 0.0), 0.82),
                "evidence_sources": list(result.get("evidence_sources") or [])
                + ["chemical_name_resolver"],
                "resolution": {
                    "resolver": str(resolution.get("resolver") or "chemical_name_resolver"),
                    "resolved_name": str(resolution.get("resolved_name") or candidate_name),
                    "source_url": str(resolution.get("source_url") or ""),
                },
            }
        )
        if resolved.get("status") == "resolved":
            return resolved
    result["name_resolution_status"] = "unavailable"
    return result


def taxonomy_motif_smiles(value: Any, *, taxonomy_profile: Any = "chemistry_general") -> str:
    """Return a taxonomy-provided motif SMILES for legacy read paths."""

    motif = _motif_from_text(
        [str(value or "")],
        _motifs_for_profile(str(taxonomy_profile or "").casefold().strip()),
    )
    if motif is None:
        return ""
    validated = _validated_contract({"status": "resolved", **motif})
    return str(validated.get("smiles") or "") if validated.get("status") == "resolved" else ""


def taxonomy_profile_has_structure_registry(taxonomy_profile: Any) -> bool:
    """Whether a taxonomy profile declares chemical structure resources."""

    profile = str(taxonomy_profile or "").casefold().strip()
    resource_name = "chemistry_general" if profile == "chemistry" else profile
    return bool(resource_name and _load_taxonomy_file(resource_name))


def taxonomy_requires_overview_structure(taxonomy_profile: Any) -> bool:
    """Whether a taxonomy explicitly requires a representative structure."""

    profile = str(taxonomy_profile or "").casefold().strip()
    resource_name = "chemistry_general" if profile == "chemistry" else profile
    return bool(
        resource_name
        and _load_taxonomy_payload(resource_name).get("overview_structure_required")
    )


def contract_smiles(contract: Any) -> str:
    """Return a validated display SMILES, or an empty string when unresolved."""

    if not isinstance(contract, dict) or str(contract.get("status") or "") != "resolved":
        return ""
    validated = _validated_contract(contract)
    return (
        str(validated.get("smiles") or "").strip()
        if validated.get("validation_status") == "validated"
        else ""
    )
