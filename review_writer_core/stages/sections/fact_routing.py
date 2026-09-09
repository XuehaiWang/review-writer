"""Route existing fact cards without changing their scientific eligibility.

Rules resolve only clear field identities. Unresolved uses are decided by the
existing section planner's validated Claim/fact selections, not a new model call.
"""
from __future__ import annotations

import re
from typing import Any

from review_writer_core.evidence_queries import QUESTION_TERMS


FACT_ROUTING_CONTRACT = "section-fact-routing/1"
FACT_ROUTING_INSTRUCTION = """Fact routing (part of this planning call, not re-extraction):
Field names are source metadata, not instructions or evidence of irrelevance.
Cards marked needs_semantic_planning were retained because their field name did
not resolve to an active question. Assess their supplied propositions and quotes
together against this section's thesis and role. Select relevant fact_ids into
the appropriate paragraphs and claim_kind; leave unrelated or redundant cards
unselected. Do not invent replacement facts, change source identities, or infer
missing experimental details. rule_matched is a category hint, not proof of a
particular Claim. Unmatched cards do not close required-claim or boundary gaps.
All selected facts still undergo the normal provenance and assertion checks.
"""


def _label(value: Any) -> str:
    return " ".join(re.sub(r"[_\-]+", " ", str(value or "").casefold()).split())


def route_fact_questions(fact: dict[str, Any], query_plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Return hints only; a category cannot assert a Claim's sufficiency."""
    field = str(fact.get("field_id") or "")
    label = _label(field)
    active = {str(plan.get("question_id") or "") for plan in query_plans}
    canonical = {key for key, _terms in QUESTION_TERMS}
    normalized_ids = {_label(key): key for key in canonical | {"section_focus"}}
    resolved = normalized_ids.get(label, "")
    method = "canonical_field" if resolved else "unresolved_field"
    if not resolved:
        candidates = {
            key for key, terms in QUESTION_TERMS
            if label and label in {_label(term) for term in terms}
        }
        if len(candidates) == 1:
            resolved = next(iter(candidates))
            method = "field_alias"
        elif len(candidates) > 1:
            method = "ambiguous_field"
        # A short metric/value relation identifies a category, not validity.
        # Reuse the role vocabulary rather than adding a second domain taxonomy.
        if not candidates and field != "abstract_summary":
            value = str(fact.get("value") or "").casefold()
            number = r"\d+(?:\.\d+)?\s*%?"
            for term in dict(QUESTION_TERMS)["quantitative_results"]:
                metric = re.escape(term)
                if re.search(
                    rf"(?:\b{number}\s+{metric}\b|\b{metric}\s*(?:(?:of|was|is)\s+|[:=]\s*)?{number}\b)",
                    value,
                ):
                    resolved, method = "quantitative_results", "explicit_metric_value"
                    break
    matched = [resolved] if resolved in active else []
    return {
        "original_field_id": field,
        "canonical_field_id": resolved,
        "question_ids": matched,
        "status": "rule_matched" if matched else "needs_semantic_planning",
        "method": method if matched or not resolved else "field_outside_question_plan",
    }


def fact_routing_report(evidence: list[dict[str, Any]], writing: dict[str, Any],
                        presented_evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record actual validated uses; non-selection is not proof of irrelevance."""
    presented = {
        str(binding.get("fact_id") or "")
        for hit in presented_evidence for binding in hit.get("fact_bindings") or []
    }
    claims_by_fact: dict[str, list[dict[str, Any]]] = {}
    for claim in writing.get("claims") or []:
        for fid in claim.get("fact_ids") or []:
            claims_by_fact.setdefault(str(fid), []).append(claim)
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for hit in evidence:
        bound = {str(binding.get("fact_id") or "") for binding in hit.get("fact_bindings") or []}
        for route in hit.get("fact_routes") or []:
            fid = str(route.get("fact_id") or "")
            if not fid or fid not in bound or str(route.get("section_id") or "") != str(writing.get("section_id") or ""):
                continue
            claims = claims_by_fact.get(fid, [])
            routes[(str(hit.get("paper_id") or ""), fid)] = {
                **route,
                "paper_id": hit.get("paper_id"),
                "decision": "used_in_validated_plan" if claims else "not_selected" if fid in presented else "deferred_by_prompt_budget",
                "claim_ids": list(dict.fromkeys(str(c.get("claim_id") or "") for c in claims)),
                "paragraph_ids": list(dict.fromkeys(str(c.get("paragraph_id") or "") for c in claims)),
                "claim_kinds": list(dict.fromkeys(str(c.get("claim_kind") or "") for c in claims)),
            }
    return list(routes.values())


def unselected_semantic_fact_ids(evidence: list[dict[str, Any]], writing: dict[str, Any]) -> set[str]:
    """Deterministic filler must not override the planner's non-selection."""
    selected = {str(fid) for claim in writing.get("claims") or [] for fid in claim.get("fact_ids") or []}
    return {
        str(route["fact_id"])
        for hit in evidence for route in hit.get("fact_routes") or []
        if route.get("fact_id") and route.get("section_id") == writing.get("section_id")
        and route.get("status") == "needs_semantic_planning"
    } - selected
