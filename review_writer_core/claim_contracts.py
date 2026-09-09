"""Shared scientific-claim, writing-requirement, and section-readiness semantics.

Blueprint generators historically stored both scientific propositions and
authoring instructions in ``review_claims``.  This module is the single
compatibility boundary that keeps authoring instructions out of evidence
retrieval while preserving genuinely source-testable legacy claims.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
import re
from typing import Any, Iterable

from .evidence_integrity import unsupported_realization_anchors
from .evidence_queries import registered_fact_field_ids
from .scientific_facts import (
    claim_assertion_ceiling,
    fact_is_usable,
    normalize_assertion_ceiling,
)


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
CURRENT_CLAIM_SUPPORT_STATUSES = {
    "supported",
    "partially_supported",
    "missing",
}
FACT_GROUNDED_BLUEPRINT_SCHEMA_VERSION = 2
ARGUMENT_CONTRACT = "review-argument/1"
ARGUMENT_FIELDS = ("claim_id", "claim_revision", "proposition", "claim_type", "allowed_assertion",
                   "fact_ids", "evidence_refs", "assertion_ceiling", "epistemic_status", "argument_basis", "source")
FACT_ROLE_CLAIM_TYPES = {
    "method_conditions": "reported_result",
    "quantitative_results": "reported_result",
    "object_input": "reported_result",
    "scope": "scope",
    "limitations": "limitation",
    "mechanism": "mechanism",
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


def _claim_support_status(value: Any) -> str:
    normalized = _text(value).casefold()
    if normalized in CURRENT_CLAIM_SUPPORT_STATUSES:
        return normalized
    if normalized == "blocked":
        return "missing"
    return "missing"


def claim_is_executable(claim: Any) -> bool:
    """Return whether a current Claim is safe to assign to a writing plan."""

    if not isinstance(claim, dict):
        return False
    return bool(
        _claim_support_status(claim.get("support_status")) == "supported"
        and _unique(claim.get("fact_ids") or [])
        and [value for value in claim.get("evidence_refs") or [] if isinstance(value, dict)]
        and normalize_assertion_ceiling(claim.get("assertion_ceiling"))
        != "context_only"
        and _text(claim.get("allowed_assertion") or claim.get("proposition"))
        and argument_support_status(claim) == "supported"
    )


def argument_fingerprint(claim):
    basis = claim.get("argument_basis") or {}
    payload = {key: claim.get(key) for key in ("claim_id", "claim_revision", "proposition", "claim_type",
        "allowed_assertion", "fact_ids", "evidence_refs", "assertion_ceiling", "epistemic_status")}
    payload["basis"] = {key: value for key, value in basis.items() if key != "verification"}
    payload["contract"] = ARGUMENT_CONTRACT
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def argument_projection(claim):
    """Carry the audited argument unchanged through the writing plan and publication."""
    return {key: deepcopy(claim[key]) for key in ARGUMENT_FIELDS if key in claim} if claim.get("argument_basis") else {}


def argument_support_status(claim):
    basis = claim.get("argument_basis") or {}
    if not basis:
        # Legacy direct reports remain usable; old comparison templates are not an audited synthesis.
        return "missing" if claim.get("claim_type") in {"cross_study_comparison", "review_synthesis"} else _claim_support_status(claim.get("support_status"))
    verdict = basis.get("verification") or {}
    if basis.get("mode") not in {"reported", "author_interpretation", "synthesis"}:
        return "missing"
    if verdict.get("status") == "rejected" or not claim.get("fact_ids"):
        return "missing"
    if (verdict.get("status") != "supported" or verdict.get("contract_version") != ARGUMENT_CONTRACT
            or verdict.get("input_fingerprint") != argument_fingerprint(claim)):
        return "partially_supported"
    return "supported"


def verify_argument(claim, *, supported, reason):
    """Register a completed source/argument verdict in one shared representation."""
    basis = claim.setdefault("argument_basis", {"mode": "reported"})
    basis["verification"] = {"status": "supported" if supported else "rejected", "reason": str(reason),
        "contract_version": ARGUMENT_CONTRACT, "input_fingerprint": argument_fingerprint(claim)}
    claim["support_status"] = "supported" if supported else "missing"
    return claim


def section_argument_readiness(section):
    claims = section.get("scientific_claims") or []
    core = [claim for claim in claims if claim.get("required_for_section") is True]
    pending = [claim for claim in core if not claim_is_executable(claim)]
    framing = section.get("section_role") in {"introduction", "conclusion"}
    eligible = framing or bool(core) and not pending
    return {"generation_eligible": eligible,
        "executable_claim_count": sum(claim_is_executable(c) for c in claims),
        "pending_claim_count": len(pending), "evidence_readiness": {
            "status": "synthesis" if framing else "ready" if eligible else "partial",
            "missing_core_claim_ids": [c["claim_id"] for c in pending],
            "reason": "" if eligible else "The section's core argument still needs support or revision."}}


def scientific_claim_evidence_state(
    claim: dict[str, Any], evidence: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Resolve one Claim only from its registered fact and evidence identities."""

    claim_id = _text(claim.get("claim_id"))
    rows = [dict(row) for row in evidence if isinstance(row, dict)]
    by_key = {
        _text(row.get("evidence_key")): row
        for row in rows
        if _text(row.get("evidence_key"))
    }
    required_keys = {
        _text(ref.get("evidence_key"))
        for ref in claim.get("evidence_refs") or []
        if isinstance(ref, dict) and _text(ref.get("evidence_key"))
    }
    required_fact_ids = set(_unique(claim.get("fact_ids") or []))
    available_fact_ids = {
        _text(binding.get("fact_id"))
        for key in required_keys
        for binding in (by_key.get(key) or {}).get("fact_bindings") or []
        if isinstance(binding, dict) and _text(binding.get("fact_id"))
    }
    claim_papers = set(_paper_ids(claim))
    evidence_papers = {
        _text((by_key.get(key) or {}).get("paper_id")) for key in required_keys
    }
    missing_keys = sorted(required_keys - set(by_key))
    missing_fact_ids = sorted(required_fact_ids - available_fact_ids)
    missing_papers = sorted(claim_papers - evidence_papers)
    if not claim_is_executable(claim):
        status = "evidence_missing"
    elif missing_keys or missing_fact_ids or missing_papers:
        status = "partially_supported" if required_keys & set(by_key) else "evidence_missing"
    else:
        status = "evidence_supported"
    return {
        "claim_id": claim_id,
        "proposition": _text(claim.get("proposition")),
        "required_for_section": bool(claim.get("required_for_section", True)),
        "status": status,
        "matched_papers": sorted(evidence_papers - {""}),
        "missing_evidence_keys": missing_keys,
        "missing_fact_ids": missing_fact_ids,
        "missing_paper_ids": missing_papers,
        "support_basis": "registered_claim_fact_bindings",
    }


def claim_planning_prompt_block(supported_claims, evidence_states, writing_requirements):
    """Keep every executable identity; omit only duplicated supported states."""
    parts = (
        ("Source-testable scientific claims permitted by current evidence", supported_claims),
        ("Unresolved scientific claim evidence states (boundaries, not prose obligations)",
         [state for state in evidence_states if state.get("status") != "evidence_supported"]),
        ("Writing requirements (authoring operations, never source propositions)", writing_requirements),
    )
    return "\n".join(
        f"{label}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for label, value in parts
    )


def _fact_evidence_refs(fact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(value)
        for value in fact.get("evidence_refs") or fact.get("support_spans") or []
        if isinstance(value, dict) and _text(value.get("evidence_key"))
    ]


def fact_is_claim_ready(fact: Any) -> bool:
    """Require the current semantic fact audit before Blueprint use."""

    if not isinstance(fact, dict) or not fact_is_usable(fact, purpose="detail"):
        return False
    verification = dict(fact.get("verification") or {})
    return bool(
        verification.get("status") == "supported"
        and verification.get("contract")
        and _text(fact.get("fact_id"))
        and _fact_evidence_refs(fact)
        and normalize_assertion_ceiling(fact.get("assertion_ceiling"))
        != "context_only"
    )


def _claim_from_fact(
    *,
    claim_id: str,
    paper_id: str,
    field_id: str,
    fact: dict[str, Any],
) -> dict[str, Any]:
    value = _text(fact.get("value"))
    evidence_refs = _fact_evidence_refs(fact)
    subject = _text(fact.get("subject"))
    predicate = _text(fact.get("predicate"))
    verification = dict(fact.get("verification") or {})
    relation_audited = bool(
        verification.get("status") == "supported"
        and verification.get("contract")
    )
    claim = {
        "claim_id": claim_id,
        "claim_revision": 1,
        "proposition": value,
        "claim_type": FACT_ROLE_CLAIM_TYPES.get(field_id, "reported_result"),
        "primary_papers": [paper_id],
        "comparison_papers": [],
        "required_fact_roles": [field_id],
        "required_for_section": True,
        "source": "blueprint_fact_card",
        "fact_ids": [_text(fact.get("fact_id"))],
        "evidence_refs": evidence_refs,
        "support_status": "supported",
        "coverage": {
            "subject": relation_audited and bool(subject or value),
            "predicate": relation_audited and bool(predicate or value),
            "value": relation_audited and bool(value),
            "qualifiers": relation_audited,
            "paper_identity": True,
        },
        "allowed_assertion": value,
        "assertion_ceiling": normalize_assertion_ceiling(
            fact.get("assertion_ceiling")
        ),
        "evidence_ceiling": _text(fact.get("evidence_ceiling")),
        "epistemic_status": _text(fact.get("epistemic_status")),
        "semantic_constraints": [
            f"paper_id={paper_id}",
            f"fact_role={field_id}",
            "Preserve the audited subject, experiment, qualifiers, and source attribution.",
        ],
    }
    claim["argument_basis"] = {"mode": "author_interpretation" if fact.get("epistemic_status") == "source_author_interpretation"
        else "reported", "comparison_basis": "", "reasoning_summary": "Direct projection of the registered source fact.",
        "counterevidence_fact_ids": []}
    return verify_argument(claim, supported=True, reason="Reused the source fact's valid evidence and attribution.")


def resolve_claim_with_fact(
    claim: dict[str, Any], fact: dict[str, Any]
) -> dict[str, Any] | None:
    """Resolve one pre-registered gap slot without changing its identity."""

    if not isinstance(claim, dict) or not isinstance(fact, dict):
        return None
    papers = _paper_ids(claim)
    roles = _unique(claim.get("required_fact_roles") or [])
    paper_id = _text(fact.get("paper_id"))
    field_id = _text(fact.get("field_id")).casefold()
    if (
        len(papers) != 1
        or paper_id != papers[0]
        or field_id not in roles
        or not fact_is_claim_ready(fact)
        or not _text(fact.get("fact_id"))
        or not _fact_evidence_refs(fact)
    ):
        return None
    resolved = _claim_from_fact(
        claim_id=_text(claim.get("claim_id")),
        paper_id=paper_id,
        field_id=field_id,
        fact=fact,
    )
    resolved["required_for_section"] = bool(
        claim.get("required_for_section", True)
    )
    resolved["source"] = "targeted_fact_repair"
    resolved["semantic_constraints"] = _unique(
        [
            *(claim.get("semantic_constraints") or []),
            *(resolved.get("semantic_constraints") or []),
        ]
    )
    return resolved


def build_fact_grounded_claims(
    *, section_id: str, section_title: str, primary_papers: Iterable[Any],
    required_fact_roles: Iterable[Any], rows_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project all usable facts as optional inputs; the planner chooses the argument."""
    claims = []
    for paper_id in _unique(primary_papers):
        for fact in (rows_by_id.get(paper_id) or {}).get("scientific_facts") or []:
            if not fact_is_claim_ready(fact) or not fact.get("fact_id") or not _fact_evidence_refs(fact):
                continue
            claim = _claim_from_fact(claim_id=section_id + "-SC-" + str(fact["fact_id"]), paper_id=paper_id,
                                     field_id=str(fact.get("field_id") or ""), fact=fact)
            claim["required_for_section"] = False
            claims.append(claim)
    return claims


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
    # A current Claim owns its paper scope.  Section-level papers are only a
    # compatibility fallback for legacy Claims that carry no paper identity.
    # Unioning the defaults into an explicit one-paper Claim makes that Claim
    # appear to require evidence from every primary paper in the section.
    primary_papers = _paper_ids(source)
    if not primary_papers:
        primary_papers = _unique(default_paper_ids)
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
        "claim_revision": int(source.get("claim_revision") or 1),
        **({"argument_basis": deepcopy(source["argument_basis"])} if source.get("argument_basis") else {}),
        "proposition": proposition,
        "claim_type": claim_type or "reported_result",
        "primary_papers": primary_papers,
        "comparison_papers": comparison_papers,
        "required_fact_roles": _unique(source.get("required_fact_roles") or []),
        "required_for_section": bool(source.get("required_for_section", True)),
        "source": _text(source.get("source")) or "blueprint",
        "fact_ids": _unique(source.get("fact_ids") or []),
        "evidence_refs": evidence_refs,
        "support_status": argument_support_status(source),
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
    # Coverage emitted by a writing model is not a semantic audit.  Only the
    # Matrix-audited Blueprint claim (or the same slot resolved by the shared
    # targeted fact repair) may carry subject/predicate/qualifier decisions
    # into this deterministic check.
    existing = (
        dict(claim.get("coverage") or {})
        if _text(claim.get("source"))
        in {
            "blueprint_fact_card",
            "blueprint_fact_comparison",
            "targeted_fact_repair",
        }
        else {}
    )
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
