"""Shared scientific-claim, writing-requirement, and section-readiness semantics.

Blueprint generators historically stored both scientific propositions and
authoring instructions in ``review_claims``.  This module is the single
compatibility boundary that keeps authoring instructions out of evidence
retrieval while preserving genuinely source-testable legacy claims.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .evidence_integrity import unsupported_realization_anchors


SCIENTIFIC_CLAIM_TYPES = {
    "reported_result",
    "comparison",
    "cross_study_comparison",
    "mechanism",
    "scope",
    "limitation",
    "foundation",
    "extension",
    "contrast",
    "review_synthesis",
}
WRITING_SIGNAL_RE = re.compile(
    r"\b(?:draft|write|synthesi[sz]e|develop|organize|structure|frame|"
    r"compare\s+(?:the\s+)?(?:assigned|selected|body[- ]section)\s+(?:papers|studies|conclusions)|"
    r"establish|show\s+how|contrast|qualify|separate|define|"
    r"use\s+the\s+assigned\s+papers|"
    r"paragraph|avoid\s+one[- ]paper|reserve\s+detailed)\b",
    re.I,
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _unique(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            text for value in values if (text := _text(value))
        )
    )


def _claim_text(raw: Any) -> str:
    if isinstance(raw, dict):
        return _text(
            raw.get("proposition")
            or raw.get("claim")
            or raw.get("text")
            or raw.get("instruction")
        )
    return _text(raw)


def _paper_ids(raw: Any, defaults: Iterable[Any] = ()) -> list[str]:
    values: list[Any] = []
    if isinstance(raw, dict):
        for key in (
            "primary_papers",
            "paper_ids",
            "citation_group",
            "comparison_papers",
            "supporting_papers",
        ):
            source = raw.get(key) or []
            if not isinstance(source, list):
                source = [source]
            for item in source:
                values.append(item.get("paper_id") if isinstance(item, dict) else item)
    return _unique([*values, *defaults])


def _is_structured_scientific_claim(raw: Any) -> bool:
    if not isinstance(raw, dict) or not _claim_text(raw):
        return False
    if raw.get("scientific_proposition") is True or "proposition" in raw:
        return True
    claim_type = _text(raw.get("claim_type") or raw.get("claim_kind")).casefold()
    if claim_type in SCIENTIFIC_CLAIM_TYPES and any(
        raw.get(key)
        for key in (
            "supporting_papers",
            "primary_papers",
            "paper_ids",
            "fact_ids",
            "evidence_refs",
            "required_fact_roles",
            "comparison_axes",
        )
    ):
        return True
    return bool(raw.get("fact_ids") or raw.get("evidence_refs"))


def _is_writing_requirement(raw: Any) -> bool:
    if isinstance(raw, dict):
        if raw.get("requirement_id") or raw.get("instruction"):
            return True
        if _text(raw.get("legacy_role")).casefold() == "writing_requirement":
            return True
    text = _claim_text(raw)
    return bool(text and WRITING_SIGNAL_RE.search(text))


def _normalize_scientific_claim(
    raw: Any,
    *,
    section_id: str,
    index: int,
    default_paper_ids: Iterable[Any] = (),
) -> dict[str, Any] | None:
    proposition = _claim_text(raw)
    if not proposition:
        return None
    source = dict(raw) if isinstance(raw, dict) else {}
    claim_type = _text(source.get("claim_type") or source.get("claim_kind"))
    primary_papers = _paper_ids(source, default_paper_ids)
    comparison_papers = _paper_ids(
        {"comparison_papers": source.get("comparison_papers") or []}
    )
    evidence_refs = [
        dict(value)
        for value in source.get("evidence_refs") or []
        if isinstance(value, dict)
    ]
    coverage = dict(source.get("coverage") or {})
    return {
        "claim_id": _text(source.get("claim_id")) or f"{section_id}-SC{index:02d}",
        "proposition": proposition,
        "claim_type": claim_type or "reported_result",
        "primary_papers": primary_papers,
        "comparison_papers": comparison_papers,
        "required_fact_roles": _unique(source.get("required_fact_roles") or []),
        "required_for_section": bool(source.get("required_for_section", True)),
        "source": _text(source.get("source")) or "blueprint",
        "fact_ids": _unique(source.get("fact_ids") or []),
        "evidence_refs": evidence_refs,
        "support_status": _text(source.get("support_status")) or "not_assessed",
        "coverage": {
            key: bool(coverage.get(key, False))
            for key in ("subject", "predicate", "value", "qualifiers", "paper_identity")
        },
        "allowed_assertion": _text(source.get("allowed_assertion")),
        "assertion_ceiling": _text(source.get("assertion_ceiling")) or "context_only",
        "evidence_ceiling": _text(source.get("evidence_ceiling")),
        "epistemic_status": _text(source.get("epistemic_status")),
        "semantic_constraints": _unique(source.get("semantic_constraints") or []),
    }


def claim_support_coverage(
    claim: dict[str, Any],
    *,
    evidence_texts: Iterable[Any] = (),
    available_fact_ids: Iterable[Any] = (),
    evidence_paper_ids: Iterable[Any] = (),
    domain_terms: Iterable[str] = (),
) -> dict[str, Any]:
    """Derive deterministic Claim coverage from existing facts and evidence.

    This function does not perform semantic matching.  It validates the pieces
    that must never be guessed by a model: referenced fact availability,
    paper identity, and realized numerical/technical anchors.  A later bounded
    matcher may fill subject/predicate/qualifier semantics, but cannot override
    these hard failures.
    """

    required_fact_ids = set(_unique(claim.get("fact_ids") or []))
    available = set(_unique(available_fact_ids))
    missing_fact_ids = sorted(required_fact_ids - available)
    claim_papers = set(
        _unique(
            [
                *(claim.get("primary_papers") or []),
                *(claim.get("paper_ids") or []),
                *(claim.get("citation_group") or []),
            ]
        )
    )
    evidence_papers = set(_unique(evidence_paper_ids))
    paper_identity = not claim_papers or claim_papers.issubset(evidence_papers)
    proposition = _text(
        claim.get("proposition") or claim.get("claim") or claim.get("allowed_assertion")
    )
    unsupported = unsupported_realization_anchors(
        proposition,
        evidence_texts,
        domain_terms=domain_terms,
    )
    value_supported = not unsupported["quantitative"]
    subject_supported = not unsupported["technical_entities"]
    existing = dict(claim.get("coverage") or {})
    coverage = {
        "subject": bool(existing.get("subject", subject_supported)) and subject_supported,
        "predicate": bool(existing.get("predicate", bool(proposition))),
        "value": bool(existing.get("value", value_supported)) and value_supported,
        "qualifiers": bool(existing.get("qualifiers", value_supported)) and value_supported,
        "paper_identity": bool(existing.get("paper_identity", paper_identity)) and paper_identity,
    }
    failed = [key for key, supported in coverage.items() if not supported]
    if missing_fact_ids:
        failed.append("fact_ids")
    if not failed and (required_fact_ids or claim.get("evidence_refs")):
        support_status = "supported"
    elif len(failed) < len(coverage) + 1 and (available or list(evidence_texts)):
        support_status = "partially_supported"
    else:
        support_status = "missing"
    return {
        "support_status": support_status,
        "coverage": coverage,
        "failed_coverage_fields": failed,
        "missing_fact_ids": missing_fact_ids,
        "unsupported_anchors": unsupported,
    }


def _normalize_writing_requirement(
    raw: Any, *, section_id: str, index: int
) -> dict[str, Any] | None:
    instruction = _claim_text(raw)
    if not instruction:
        return None
    source = dict(raw) if isinstance(raw, dict) else {}
    requirement_type = _text(source.get("type"))
    if not requirement_type:
        requirement_type = (
            "cross_study_synthesis"
            if re.search(r"\b(?:compare|synthesi[sz]e)\b", instruction, re.I)
            else "authoring_constraint"
        )
    return {
        "requirement_id": _text(source.get("requirement_id"))
        or f"WR-{section_id}-{index:02d}",
        "type": requirement_type,
        "instruction": instruction,
        "source": _text(source.get("source")) or "blueprint",
    }


def normalize_section_claim_contract(section: dict[str, Any]) -> dict[str, Any]:
    """Return one normalized claim contract for current and legacy Blueprints.

    Explicit ``scientific_claims`` and ``writing_requirements`` win.  Legacy
    ``review_claims`` are classified conservatively: only structurally
    source-testable rows become scientific claims; instruction-like rows become
    writing requirements; ambiguous rows remain visible but cannot create a
    required evidence query.
    """

    section_id = _text(section.get("section_id")) or "S00"
    default_papers = _unique(
        [
            *(section.get("primary_papers") or []),
            *(section.get("major_papers") or []),
        ]
    )
    scientific: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []

    has_explicit_contract = (
        "scientific_claims" in section or "writing_requirements" in section
    )
    for index, raw in enumerate(section.get("scientific_claims") or [], start=1):
        normalized = _normalize_scientific_claim(
            raw,
            section_id=section_id,
            index=index,
            default_paper_ids=default_papers,
        )
        if normalized:
            scientific.append(normalized)
    for index, raw in enumerate(section.get("writing_requirements") or [], start=1):
        normalized = _normalize_writing_requirement(
            raw, section_id=section_id, index=index
        )
        if normalized:
            requirements.append(normalized)

    if not has_explicit_contract:
        for raw in section.get("review_claims") or []:
            explicitly_scientific = isinstance(raw, dict) and (
                raw.get("scientific_proposition") is True or "proposition" in raw
            )
            if explicitly_scientific or (
                _is_structured_scientific_claim(raw)
                and not _is_writing_requirement(raw)
            ):
                normalized = _normalize_scientific_claim(
                    raw,
                    section_id=section_id,
                    index=len(scientific) + 1,
                    default_paper_ids=default_papers,
                )
                if normalized:
                    scientific.append(normalized)
            elif _is_writing_requirement(raw):
                normalized = _normalize_writing_requirement(
                    raw,
                    section_id=section_id,
                    index=len(requirements) + 1,
                )
                if normalized:
                    requirements.append(normalized)
            elif (text := _claim_text(raw)):
                unclassified.append(
                    {
                        "text": text,
                        "source": "legacy_review_claim",
                        "reason": "not_structurally_source_testable",
                    }
                )

    return {
        "scientific_claims": scientific,
        "writing_requirements": requirements,
        "legacy_unclassified_claims": unclassified,
    }


def derive_section_readiness(
    *,
    generation_mode: str,
    required_claim_states: Iterable[dict[str, Any]] = (),
    structure_gaps: Iterable[Any] = (),
    depth_sufficient: bool = True,
    failed: bool = False,
) -> dict[str, Any]:
    """Derive one read-only scientific readiness from existing section facts."""

    claim_states = [dict(item) for item in required_claim_states if isinstance(item, dict)]
    missing_claim_ids = [
        _text(item.get("claim_id"))
        for item in claim_states
        if bool(item.get("required_for_section", True))
        and _text(item.get("status")).casefold()
        in {"evidence_missing", "insufficient", "retrieval_not_found"}
        and _text(item.get("claim_id"))
    ]
    gaps = _unique(structure_gaps)
    mode = _text(generation_mode).casefold() or "standard"
    if failed:
        status = "failed"
    elif missing_claim_ids:
        status = "needs_evidence_repair"
    elif gaps:
        status = "needs_structure_repair"
    elif mode == "safe_evidence_fallback":
        status = "provider_fallback"
    elif not depth_sufficient:
        status = "evidence_safe_but_shallow"
    else:
        status = "scientific_complete"
    return {
        "status": status,
        "generation_mode": mode,
        "missing_required_claim_ids": missing_claim_ids,
        "structure_gaps": gaps,
        "depth_sufficient": bool(depth_sufficient),
        "derived": True,
    }
