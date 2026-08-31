"""Shared normalization for evidence-backed review classification axes.

The query planner may use a familiar structured metadata field as the ID for a
cross-cutting discussion dimension.  That is harmless during retrieval but it
becomes misleading once Matrix facts and Blueprint routes use the ID as an
academic contract.  This module repairs only high-confidence semantic cases;
it does not classify papers or infer a category from missing evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


CLASSIFICATION_CONTRACT_VERSION = 2
STEREOCHEMICAL_AXIS_ID = "stereochemical_regime"

_STEREOCHEMICAL_AXIS = re.compile(
    r"\b(?:stereochem(?:ical|istry)|stereoselectiv(?:e|ity)|chirality\s+mode|"
    r"asymmetric\s+(?:mode|regime)|racemic\s+(?:versus|vs\.?|and)\s+"
    r"(?:enantioselective|asymmetric))\b",
    re.I,
)
_RACEMIC_PARTITION = re.compile(
    r"(?:\bracemic\b|\bracemate\b|\bracemic\s+mixture\b|\(\s*[±∓]\s*\))",
    re.I,
)
_ENANTIOSELECTIVE_PARTITION = re.compile(
    r"\b(?:enantioselectiv(?:e|ity)|enantioenriched|asymmetric\s+synth(?:esis|etic)|"
    r"optically\s+active|enantiomeric\s+(?:excess|ratio)|chiral\s+(?:catalyst|ligand))\b|"
    r"(?<![A-Za-z])ee(?![A-Za-z])|(?<![A-Za-z])er(?![A-Za-z])",
    re.I,
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _partition_text(partition: dict[str, Any]) -> str:
    return " ".join(
        _compact(value)
        for value in [
            partition.get("label"),
            *(partition.get("aliases") or []),
            *(partition.get("positive_discriminators") or []),
        ]
        if _compact(value)
    )


def axis_is_stereochemical_regime(axis: dict[str, Any]) -> bool:
    """Return true only for an explicit stereochemical contrast.

    A single word such as ``chiral`` is deliberately insufficient.  A generic
    reaction axis is repaired only when its label names stereochemistry or its
    partitions positively contain both a racemic and an enantioselective side.
    """

    descriptor = " ".join(
        _compact(axis.get(key)) for key in ("axis_id", "label", "source_surface")
    )
    if str(axis.get("axis_id") or "") == STEREOCHEMICAL_AXIS_ID:
        return True
    if _STEREOCHEMICAL_AXIS.search(descriptor):
        return True
    partition_texts = [
        _partition_text(partition)
        for partition in axis.get("partitions") or []
        if isinstance(partition, dict)
    ]
    return any(_RACEMIC_PARTITION.search(text) for text in partition_texts) and any(
        _ENANTIOSELECTIVE_PARTITION.search(text) for text in partition_texts
    )


def normalize_classification_axis_semantics(axis: dict[str, Any]) -> dict[str, Any]:
    """Repair a classification-axis contract without changing its evidence role."""

    normalized = deepcopy(axis)
    if not axis_is_stereochemical_regime(normalized):
        return normalized

    normalized["axis_id"] = STEREOCHEMICAL_AXIS_ID
    normalized["label"] = "Stereochemical regime"
    normalized["semantic_repair"] = {
        "status": "auto_repaired",
        "reason": (
            "Racemic versus enantioselective/asymmetric evidence is a "
            "stereochemical regime, not a reaction-type partition."
        ),
    }
    for partition in normalized.get("partitions") or []:
        if not isinstance(partition, dict):
            continue
        text = _partition_text(partition)
        aliases = list(dict.fromkeys(
            _compact(value)
            for value in [partition.get("label"), *(partition.get("aliases") or [])]
            if _compact(value)
        ))
        positive = list(dict.fromkeys(
            _compact(value)
            for value in partition.get("positive_discriminators") or []
            if _compact(value)
        ))
        ambiguous = list(dict.fromkeys(
            _compact(value)
            for value in partition.get("negative_or_ambiguous_signals") or []
            if _compact(value)
        ))
        if _RACEMIC_PARTITION.search(text):
            aliases = list(dict.fromkeys(["racemic", "racemate", "(±)", *aliases]))
            positive = list(dict.fromkeys([
                "explicitly reported racemic product",
                "racemate or racemic mixture",
                "(±) product designation",
                *positive,
            ]))
            ambiguous = list(dict.fromkeys([
                "missing ee or er",
                "absence of a chiral catalyst or ligand",
                "stereochemistry not reported",
                *ambiguous,
            ]))
        elif _ENANTIOSELECTIVE_PARTITION.search(text):
            aliases = list(dict.fromkeys([
                "enantioselective",
                "asymmetric",
                "enantioenriched",
                "optically active",
                *aliases,
            ]))
            positive = list(dict.fromkeys([
                "reported ee or er",
                "explicit enantioselective or asymmetric synthesis",
                "reported optically active or enantioenriched product",
                "explicit chiral catalyst or chiral ligand induction",
                *positive,
            ]))
        partition["aliases"] = aliases[:5]
        partition["positive_discriminators"] = positive[:5]
        partition["negative_or_ambiguous_signals"] = ambiguous[:4]
    return normalized


def normalize_classification_axes_semantics(
    axes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize axes and remove exact duplicate IDs while preserving order."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for axis in axes:
        if not isinstance(axis, dict):
            continue
        repaired = normalize_classification_axis_semantics(axis)
        axis_id = _compact(repaired.get("axis_id"))
        if not axis_id or axis_id in seen:
            continue
        seen.add(axis_id)
        normalized.append(repaired)
    return normalized


def axis_requires_formal_route(axis: dict[str, Any]) -> bool:
    return str(axis.get("axis_role") or "") in {
        "primary_organization",
        "required_independent_discussion",
    }


def _contract_axis(axis: dict[str, Any]) -> dict[str, Any]:
    """Return only stable, upstream-owned fields of an axis contract.

    Matrix coverage and review-state fields are deliberately excluded.  They
    describe how much evidence is currently available, not what the selected
    academic organization means, and therefore must not silently rewrite the
    contract fingerprint after every extraction run.
    """

    return {
        key: deepcopy(value)
        for key, value in axis.items()
        if key not in {"evidence_coverage", "role_status"}
    }


def _synthetic_primary_axis(
    axis_id: str,
    *,
    source_axis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label = axis_id.replace("_", " ").strip().title()
    if axis_id == "reaction_type":
        label = "Reaction type"
    return {
        "axis_id": axis_id,
        "label": label,
        "source_surface": _compact(
            (source_axis or {}).get("source_surface") or axis_id
        ),
        "source_type": _compact(
            (source_axis or {}).get("source_type") or "explicit_topic"
        ),
        "axis_role": "primary_organization",
        "mutual_exclusivity": "partially_overlapping",
        "heading_requirement": "primary_heading",
        "recommendation_rationale": (
            "Preserve the explicitly selected primary hierarchy in the shared "
            "classification contract."
        ),
        "partitions": [],
        "axis_introduction": {
            "status": "canonicalized_from_primary_hint",
            "source_axis_id": _compact((source_axis or {}).get("axis_id")),
            "reason": (
                "The selected primary hierarchy was not represented by its own "
                "upstream axis object."
            ),
        },
    }


def canonical_classification_contract(
    axes: list[dict[str, Any]] | None,
    *,
    primary_axis_hint: Any = "",
    source: Any = "",
) -> dict[str, Any]:
    """Build one deterministic classification contract for every workflow stage.

    The contract owns academic axis semantics and heading roles.  Discovery,
    Matrix, Outline and Blueprint can add evidence or presentation metadata,
    but they should carry this same fingerprint unless the user request or the
    evidence-backed primary recommendation genuinely changes.
    """

    normalized = normalize_classification_axes_semantics(
        [_contract_axis(axis) for axis in axes or [] if isinstance(axis, dict)]
    )
    hint = _compact(primary_axis_hint)

    # Some older query plans labelled a racemic/enantioselective partition as
    # reaction_type.  Semantic repair correctly renames that dimension, but it
    # must not erase an explicit request to organize the review by reaction
    # type.  Split the two dimensions once, here, so every downstream stage
    # receives the same repaired contract.
    hinted_axis = next(
        (axis for axis in normalized if _compact(axis.get("axis_id")) == hint),
        None,
    )
    existing_primary = next(
        (
            axis
            for axis in normalized
            if _compact(axis.get("axis_role")) == "primary_organization"
        ),
        None,
    )
    if hint and hinted_axis is None:
        if existing_primary is not None:
            existing_primary["axis_role"] = (
                "required_independent_discussion"
                if _compact(existing_primary.get("source_type")) == "explicit_topic"
                else "comparison_dimension"
            )
            existing_primary["heading_requirement"] = (
                "secondary_heading"
                if existing_primary["axis_role"]
                == "required_independent_discussion"
                else "comparison_only"
            )
        normalized.insert(
            0,
            _synthetic_primary_axis(hint, source_axis=existing_primary),
        )
        hinted_axis = normalized[0]

    primary = hinted_axis or existing_primary or (normalized[0] if normalized else None)
    for axis in normalized:
        if axis is primary:
            axis["axis_role"] = "primary_organization"
            axis["heading_requirement"] = "primary_heading"
            continue
        if _compact(axis.get("axis_role")) == "primary_organization":
            axis["axis_role"] = (
                "required_independent_discussion"
                if _compact(axis.get("source_type")) == "explicit_topic"
                else "comparison_dimension"
            )
            axis["heading_requirement"] = (
                "secondary_heading"
                if axis["axis_role"] == "required_independent_discussion"
                else "comparison_only"
            )

    if primary is not None and normalized and normalized[0] is not primary:
        normalized.remove(primary)
        normalized.insert(0, primary)

    primary_axis_id = _compact((primary or {}).get("axis_id"))
    required_route_axis_ids = [
        _compact(axis.get("axis_id"))
        for axis in normalized
        if axis_requires_formal_route(axis) and _compact(axis.get("axis_id"))
    ]
    fingerprint_input = {
        "contract_version": CLASSIFICATION_CONTRACT_VERSION,
        "primary_axis_id": primary_axis_id,
        "required_route_axis_ids": required_route_axis_ids,
        "axes": normalized,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "contract_version": CLASSIFICATION_CONTRACT_VERSION,
        "source": _compact(source) or "canonical_axis_contract",
        "primary_axis_id": primary_axis_id,
        "required_route_axis_ids": required_route_axis_ids,
        "section_partition_policy": "single_primary_axis",
        "minimum_body_papers": 2,
        "single_paper_section_policy": "merge_unless_scientifically_justified",
        "axes": normalized,
        "fingerprint": fingerprint,
    }


def classification_contract_from_document(
    document: dict[str, Any] | None,
    *,
    primary_axis_hint: Any = "",
    source: Any = "",
) -> dict[str, Any]:
    """Read a current or legacy document into the canonical contract shape."""

    payload = document if isinstance(document, dict) else {}
    existing = payload.get("classification_contract")
    axes = (
        existing.get("axes")
        if isinstance(existing, dict) and isinstance(existing.get("axes"), list)
        else payload.get("classification_axes") or []
    )
    hint = (
        primary_axis_hint
        or (
            existing.get("primary_axis_id")
            if isinstance(existing, dict)
            else ""
        )
    )
    return canonical_classification_contract(
        axes,
        primary_axis_hint=hint,
        source=source or (existing or {}).get("source") or "legacy_document_upgrade",
    )
