"""Shared review-fact requirements and readiness semantics.

The extraction task status answers whether a worker completed.  Review
readiness answers whether the source-addressable facts required by the current
review question are present.  Keeping the two concepts separate prevents a
successfully completed extraction from being presented as a complete evidence
record.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .evidence_queries import COMPARISON_FIELD_IDS, QUESTION_TERMS


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
    if status == "needs_human_review" and re.search(
        r"\b(?:conflict|contradict|ambiguous|identity mismatch|inconsistent)\b",
        conflict_text,
    ):
        return "conflict"
    if list(source_evidence_refs):
        return "binding_mismatch"
    if source_ready:
        return "extraction_miss"
    if status in {"unsupported", "needs_human_review", "partially_supported"}:
        return "true_evidence_gap"
    return "none"


def _normalized_text(values: Iterable[Any]) -> str:
    return " ".join(
        " ".join(str(value or "").casefold().split()) for value in values
    )


def required_fact_roles(
    *values: Any,
    minimum_roles: Iterable[str] = DEFAULT_REVIEW_FACT_ROLES,
) -> list[str]:
    """Infer discipline-neutral fact roles from a Topic or section question.

    Terms come from the same query vocabulary used by the evidence retriever,
    so Blueprint requirements and retrieval cannot silently drift apart.
    """

    text = _normalized_text(values)
    required = [
        role for role in minimum_roles if role in COMPARISON_FIELD_IDS
    ]
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
        if role not in COMPARISON_FIELD_IDS or role in supported:
            continue
        if not str(raw.get("value") or "").strip():
            continue
        if not raw.get("evidence_refs"):
            continue
        if str(raw.get("support_level") or "").casefold() in {
            "coverage_only",
            "neighbor_context",
            "context_only",
        }:
            continue
        supported.append(role)
    return supported


def fact_readiness_report(
    *,
    facts: Iterable[Any],
    required_roles: Iterable[str],
    extraction_status: str,
    failed_fields: Iterable[Any] = (),
) -> dict[str, Any]:
    """Build one portable readiness report without inventing negative facts."""

    required = list(
        dict.fromkeys(
            role
            for role in (str(value or "").strip() for value in required_roles)
            if role in COMPARISON_FIELD_IDS
        )
    )
    fact_rows = [row for row in facts if isinstance(row, dict)]
    supported = supported_fact_roles(fact_rows)
    supported_set = set(supported)
    incomplete_roles = {
        str(row.get("field_id") or "").strip()
        for row in fact_rows
        if str(row.get("field_id") or "").strip() in COMPARISON_FIELD_IDS
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
        for role in COMPARISON_FIELD_IDS
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
    return {
        "review_readiness": readiness,
        "required_fact_roles": required,
        "supported_fact_roles": [role for role in required if role in supported_set],
        "missing_fact_roles": missing,
        "field_states": field_states,
    }
