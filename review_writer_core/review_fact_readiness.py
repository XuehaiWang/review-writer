"""Shared review-fact requirements and readiness semantics.

The extraction task status answers whether a worker completed.  Review
readiness answers whether the source-addressable facts required by the current
review question are present.  Keeping the two concepts separate prevents a
successfully completed extraction from being presented as a complete evidence
record.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from .evidence_queries import COMPARISON_FIELD_IDS, QUESTION_TERMS, registered_fact_field_ids
from .scientific_facts import (FACT_VALIDATION_VERSION, fact_is_classification, fact_is_usable,
                               fact_usage, fact_needs_verification, review_fingerprint)


DEFAULT_REVIEW_FACT_ROLES: tuple[str, ...] = (
    "object_input",
    "method_conditions",
    "quantitative_results",
    "scope",
)


FIGURE_CALLOUT_SUBJECT_RE = re.compile(
    r"(?:^|\b(?:as|also)\s+(?:shown|summarized|illustrated|depicted)\s+in\s+)"
    r"(?:figure|fig\.?|scheme|table)\s+[A-Za-z0-9][A-Za-z0-9.:-]*\b",
    re.IGNORECASE,
)
FIGURE_CALLOUT_PREDICATE_RE = re.compile(
    r"\b(?:summari[sz]es?|illustrates?|depicts?|shows?|presents?|maps?|"
    r"provides?|compares?|highlights?|visuali[sz]es?|is\s+(?:shown|presented|"
    r"summarized|illustrated|depicted))\b",
    re.IGNORECASE,
)
FIGURE_CALLOUT_SCIENTIFIC_PREDICATE_RE = re.compile(
    r"\b(?:affords?|gives?|yields?|produces?|converts?|cataly[sz]es?|"
    r"establishes?|demonstrates?|proves?|confirms?|increases?|decreases?|"
    r"outperforms?|reacts?|forms?|requires?)\b",
    re.IGNORECASE,
)
STRONG_NEGATIVE_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|did\s+not|cannot|could\s+not|never|"
    r"fails?\s+to|failed\s+to|no\s+(?:evidence|effect|reaction|product|"
    r"measurement|data|discussion)|not\s+(?:reported|observed|measured|"
    r"defined|established|demonstrated|identified|assigned|discussed))\b",
    re.IGNORECASE,
)
NEGATIVE_SOURCE_CUE_RE = re.compile(
    r"\b(?:not|no|none|neither|without|lack(?:s|ed|ing)?|fail(?:s|ed)?\s+to|"
    r"cannot|could\s+not|never)\b",
    re.IGNORECASE,
)
NEGATIVE_STOPWORDS = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "study",
    "paper",
    "report",
    "reported",
    "source",
    "does",
    "did",
    "not",
    "no",
    "cannot",
    "could",
    "establish",
    "established",
    "show",
    "shown",
}


def negative_claim_eligibility(
    fact_state: Any,
    checked_sources: Iterable[Any] = (),
) -> bool:
    """Allow public source-absence wording only after explicit source review."""

    state = str(fact_state or "").strip().casefold()
    checked = {
        str(value or "").strip().casefold()
        for value in checked_sources
        if str(value or "").strip()
    }
    return state == "source_verified_not_reported" and bool(checked)


def is_figure_callout_only(value: Any) -> bool:
    """Return whether text is placement/callout prose rather than a paper fact.

    A sentence such as ``Figure 2 summarizes the representative reactions`` is
    owned by the figure insertion validator.  A sentence that also asserts a
    result (for example, ``Figure 2 shows that catalyst A gives 95% yield``)
    remains a scientific claim and must still pass source checking.
    """

    text = " ".join(str(value or "").split()).strip()
    if not text or not FIGURE_CALLOUT_SUBJECT_RE.search(text):
        return False
    if not FIGURE_CALLOUT_PREDICATE_RE.search(text):
        return False
    if FIGURE_CALLOUT_SCIENTIFIC_PREDICATE_RE.search(text):
        return False
    return not bool(
        re.search(
            r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:%|mol\s*%|°\s*C|K|h|min|"
            r"equiv|eq\.?|M|mM|bar|atm|MPa|mg|g|mmol|mol|mL|L)(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        )
    )


def is_strong_negative_claim(value: Any) -> bool:
    """Identify paper-level absence or non-establishment language."""

    return bool(STRONG_NEGATIVE_RE.search(" ".join(str(value or "").split())))


def negative_claim_policy(
    claim: Any,
    *,
    evidence_texts: Iterable[Any] = (),
    fact_state: Any = "",
    checked_sources: Iterable[Any] = (),
) -> str:
    """Return the safe policy for a negative scientific assertion.

    ``explicit_source_statement`` is intentionally conservative.  A retrieval
    miss is never upgraded to a publication-level absence statement.  When no
    explicit negative source language can be tied to the same scientific
    terms, the caller must use a source-bounded formulation instead.
    """

    text = " ".join(str(claim or "").split()).strip()
    if not is_strong_negative_claim(text):
        return "not_negative"
    if negative_claim_eligibility(fact_state, checked_sources):
        return "closed_structure_absence"
    claim_terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text)
        if token.casefold() not in NEGATIVE_STOPWORDS
    }
    for raw in evidence_texts:
        source = " ".join(str(raw or "").split()).strip()
        if not source or not NEGATIVE_SOURCE_CUE_RE.search(source):
            continue
        source_terms = {
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", source)
            if token.casefold() not in NEGATIVE_STOPWORDS
        }
        if len(claim_terms & source_terms) >= 2:
            return "explicit_source_statement"
    return "scope_limited_rewrite"


def evidence_problem_type(
    *,
    unsupported_claims: Iterable[Any] = (),
    source_check_status: Any = "not_assessed",
    source_evidence_refs: Iterable[Any] = (),
    source_ready: bool = False,
    evidence_texts: Iterable[Any] = (),
) -> str:
    """Classify the earliest evidence-chain root without inventing certainty."""

    claims = [
        " ".join(str(value or "").split()).strip()
        for value in unsupported_claims
        if " ".join(str(value or "").split()).strip()
    ]
    status = str(source_check_status or "not_assessed").casefold()
    if not claims and status in {"verified", "not_applicable", "not_assessed"}:
        return "none"
    if claims and all(
        negative_claim_policy(claim, evidence_texts=evidence_texts)
        == "scope_limited_rewrite"
        for claim in claims
        if is_strong_negative_claim(claim)
    ) and any(is_strong_negative_claim(claim) for claim in claims):
        return "unqualified_negative_claim"
    conflict_text = " ".join(claims).casefold()
    if status == "contradicted":
        return "conflict"
    if status == "needs_human_review" and re.search(
        r"\b(?:conflict|contradict|ambiguous|identity mismatch|inconsistent)\b",
        conflict_text,
    ):
        return "conflict"
    if list(source_evidence_refs):
        return "binding_mismatch"
    if source_ready:
        return "extraction_miss"
    if status in {
        "unsupported",
        "needs_human_review",
        "partially_supported",
        "not_found_in_checked_scope",
        "contradicted",
    }:
        return "true_evidence_gap"
    return "none"


def _normalized_text(values: Iterable[Any]) -> str:
    return " ".join(
        " ".join(str(value or "").casefold().split()) for value in values
    )


def required_fact_roles(
    *values: Any,
    minimum_roles: Iterable[str] = (),
) -> list[str]:
    """Infer discipline-neutral fact roles from a Topic or section question.

    Terms come from the same query vocabulary used by the evidence retriever,
    so Blueprint requirements and retrieval cannot silently drift apart.
    """

    text = _normalized_text(values)
    minimum_roles = list(minimum_roles)
    registered = registered_fact_field_ids(required_roles=minimum_roles)
    required = [role for role in minimum_roles if role in registered]
    for role, terms in QUESTION_TERMS:
        if role in required:
            continue
        if any(
            re.search(rf"(?<![\w-]){re.escape(str(term).casefold())}(?![\w-])", text)
            for term in terms
        ):
            required.append(role)
    return required


def supported_fact_roles(facts: Iterable[Any]) -> list[str]:
    supported: list[str] = []
    for raw in facts:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("field_id") or "").strip()
        if not role or fact_is_classification(raw) or role in supported:
            continue
        if not fact_is_usable(raw, purpose="detail"):
            continue
        supported.append(role)
    return supported


def fact_readiness_report(
    *,
    facts: Iterable[Any],
    required_roles: Iterable[str],
    extraction_status: str,
    failed_fields: Iterable[Any] = (),
    baseline: bool = False,
    topic: str = "",
) -> dict[str, Any]:
    """Build one portable readiness report without inventing negative facts."""

    required_roles = list(required_roles)
    allowed = registered_fact_field_ids(required_roles=required_roles)
    required = list(
        dict.fromkeys(
            role
            for role in (str(value or "").strip() for value in required_roles)
            if role in allowed
        )
    )
    all_rows = [row for row in facts if isinstance(row, dict)]
    # Corrected candidates remain available as history, not current coverage.
    fact_rows = [row for row in all_rows if not row.get("superseded_by_fact_id")]
    supported = supported_fact_roles(fact_rows)
    supported_set = set(supported)
    if baseline:
        allowed = registered_fact_field_ids(required_roles=[*required, *(row.get("field_id") for row in fact_rows)])
    incomplete_roles = {
        str(row.get("field_id") or "").strip()
        for row in fact_rows
        if str(row.get("field_id") or "").strip() in allowed
        and str(row.get("value") or "").strip()
        and str(row.get("field_id") or "").strip() not in supported_set
    }
    verified_not_reported_roles = {
        str(row.get("field_id") or "").strip()
        for row in fact_rows
        if str(row.get("source_status") or "").casefold()
        == "source_verified_not_reported"
    }
    failed = {
        str(value or "").strip()
        for value in failed_fields
        if str(value or "").strip()
    }
    missing = [role for role in required if role not in supported_set]
    field_states = {
        role: (
            "supported"
            if role in supported_set
            else "source_verified_not_reported"
            if role in verified_not_reported_roles
            else "reported_but_incomplete"
            if role in incomplete_roles
            else "retrieval_not_found"
            if role in failed or role in missing
            else "not_requested"
        )
        for role in allowed
    }
    status = str(extraction_status or "pending").casefold()
    readiness = (
        "source_not_established"
        if status in {"pending", "running", "failed"} and not supported
        else "complete"
        if required and not missing
        else "partial"
        if supported
        else "source_not_established"
    )
    uses = coverage_by_use(fact_rows, ([{
        "use_id": "research_contribution", "purpose": "Explain this paper's contribution to the review question",
        "required": True, "usage": "background", "field_ids": [],
    }] if baseline else [{"use_id": role, "purpose": role, "required": True,
                           "field_ids": [role]} for role in required]), topic=topic)
    if baseline:
        readiness = ("complete" if uses and all(use["status"] == "supported" for use in uses)
                     else "partial" if any(fact_is_usable(f) for f in fact_rows)
                     else "source_not_established")
    return {
        "review_readiness": readiness,
        "coverage_basis": "baseline_contribution" if baseline else "requested_fact_roles_not_full_scientific_coverage",
        "coverage_by_use": uses,
        "fact_count": len(fact_rows),
        "superseded_fact_count": len(all_rows) - len(fact_rows),
        "scientific_fact_count": sum(not fact_is_classification(fact) for fact in fact_rows),
        "classification_fact_count": sum(fact_is_classification(fact) for fact in fact_rows),
        "background_fact_count": sum(fact_usage(fact) == "background" for fact in fact_rows),
        "usable_fact_count": sum(fact_is_usable(fact) for fact in fact_rows),
        "relation_checked_fact_count": sum(
            (fact.get("verification") or {}).get("contract") == FACT_VALIDATION_VERSION
            and (fact.get("verification") or {}).get("status") == "supported" for fact in fact_rows
        ),
        "required_fact_roles": required,
        "supported_fact_roles": supported if baseline else [role for role in required if role in supported_set],
        "missing_fact_roles": missing,
        "field_states": field_states,
    }


def coverage_by_use(facts, requirements, *, topic="", section_id=None):
    """Project explicit uses from one fact snapshot; never write chapter needs to Matrix."""
    facts = [f for f in facts if isinstance(f, dict) and not f.get("superseded_by_fact_id")]
    output = []
    for demand in requirements:
        fields = list(dict.fromkeys(demand.get("field_ids") or []))
        requested_ids = set(demand.get("fact_ids") or [])
        selected = [f for f in facts if (not fields or f.get("field_id") in fields)
                    and (not requested_ids or f.get("fact_id") in requested_ids)]
        usable = [f for f in selected if fact_is_usable(f, purpose=demand.get("usage", "detail"))]
        missing = [field for field in fields if not any(f.get("field_id") == field for f in usable)]
        complete = bool(usable) and not missing and requested_ids.issubset({f.get("fact_id") for f in usable})
        fingerprint = hashlib.sha256(json.dumps({"topic": topic, "demand": demand,
            "facts": [(f.get("fact_id"), review_fingerprint(f), f.get("verification"), f.get("assertion_ceiling"))
                      for f in sorted(selected, key=lambda f: str(f.get("fact_id")))],
            "contract": FACT_VALIDATION_VERSION}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        output.append({"use_id": str(demand["use_id"]), "section_id": section_id,
            "claim_ids": list(demand.get("claim_ids") or []), "purpose": str(demand.get("purpose") or ""),
            "required": demand.get("required") is True, "fact_ids": [f["fact_id"] for f in usable if f.get("fact_id")],
            "status": "supported" if complete else "partial" if usable else "unavailable",
            "missing_requirements": missing, "reason": "" if complete else "The requested use lacks verified supporting facts.",
            "dependency_fingerprint": fingerprint})
    return output


def fact_processing_state(facts, enrichment):
    """Execution, audit and recovery status independent of scientific coverage."""
    facts = [f for f in facts if isinstance(f, dict) and not f.get("superseded_by_fact_id")]
    pending = [f for f in facts if fact_needs_verification(f)]
    profile = enrichment.get("fact_extraction_profile") or {}
    stop = str(profile.get("stop_reason") or "")
    extraction = "completed" if facts or stop in {"checks_completed", "no_new_evidence", "supplement_budget_reached"} else (
        "failed" if enrichment.get("status") == "failed" else "pending")
    recovery_requested = any(q.get("source_recovery") for q in profile.get("unresolved_requests") or [])
    recovery = ("failed" if profile.get("source_recovery_errors") else "pending"
                if recovery_requested and stop in {"retrieval_unavailable", "provider_or_budget_unavailable"}
                else "completed") if recovery_requested or profile.get("source_recovery_errors") else "not_required"
    return {"processing": {"extraction": extraction,
            "verification": "pending" if pending else "completed" if facts else "not_required",
            "source_recovery": recovery},
            "pending_fact_count": len(pending), "verified_fact_count": len(facts) - len(pending)}


def fact_processing_complete(facts, enrichment):
    state = fact_processing_state(facts, enrichment)["processing"]
    stop = (enrichment.get("fact_extraction_profile") or {}).get("stop_reason")
    return (all(value in {"completed", "not_required"} for value in state.values())
            and stop not in {"provider_or_budget_unavailable", "retrieval_unavailable", "extraction_failed"})
