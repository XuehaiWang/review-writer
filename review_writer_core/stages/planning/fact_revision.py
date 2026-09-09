"""Exact, source-bound fact revisions; no extraction, inference or I/O."""

from __future__ import annotations

from copy import deepcopy

from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION, review_fingerprint
from review_writer_core.review_fact_readiness import fact_readiness_report, fact_processing_state
from review_writer_core.evidence_queries import COMPARISON_FIELD_IDS


def revise_fact(current, submitted):
    value = str(submitted.get("value", current.get("value")) or "").strip()
    ceiling = str(submitted.get("evidence_ceiling", current.get("evidence_ceiling")) or "").strip()
    if not value or len(value) > 4000 or len(ceiling) > 2000:
        raise ValueError("A Matrix fact edit has an invalid value or evidence ceiling.")
    if value == current.get("value") and ceiling == str(current.get("evidence_ceiling") or ""):
        return deepcopy(current)
    revised = deepcopy(current)
    for name in ("verification", "superseded_by_fact_id", "source_recovery_request_id",
                 "correction_of_fact_id", "source_status", "confidence"):
        revised.pop(name, None)
    revised.update(value=value, evidence_ceiling=ceiling, subject="", predicate="",
                   normalized_value=value, unit="", qualifiers={}, experiment_id="",
                   human_checked=False, review_status="pending_verification",
                   validation_contract=FACT_VALIDATION_VERSION,
                   revision_of_fact_id=current["fact_id"],
                   revision_assertion_ceiling=current.get("revision_assertion_ceiling")
                       or current.get("assertion_ceiling") or "context_only",
                   support_level="context_only", assertion_ceiling="context_only_until_relation_verified")
    revised["extraction"] = {"mode": "human_revision_pending", "revised_from_fact_id": current["fact_id"]}
    fingerprint = review_fingerprint(revised)
    revised["fact_id"] = "MF-" + fingerprint[:20].upper()
    revised["verification"] = {"status": "pending", "input_fingerprint": fingerprint}
    return revised


def pending_revisions(row):
    return [fact for fact in row.get("scientific_facts") or []
            if fact.get("revision_of_fact_id")
            and (fact.get("verification") or {}).get("status") in {"pending", "unavailable"}]


def refresh_row_facts(row):
    facts = row.get("scientific_facts") or []
    previous = row.get("fact_enrichment") or {}
    pending = pending_revisions(row)
    status = "pending" if pending else "partial"
    readiness = fact_readiness_report(facts=facts, required_roles=previous.get("required_fact_roles") or [],
                                     extraction_status=status, failed_fields=previous.get("failed_fields") or [], baseline=True)
    if not pending and readiness["review_readiness"] == "complete":
        status = "complete"
    row["fact_enrichment"] = {**previous, **readiness, "status": status,
        "extraction_status": status, "review_status": "needs_review" if pending else "auto_resolved",
        "pending_revision_fact_ids": [fact["fact_id"] for fact in pending]}
    row["fact_enrichment"].update(fact_processing_state(facts, row["fact_enrichment"]))
    row["comparison_evidence"] = {role: [dict(fact) for fact in facts if fact.get("field_id") == role]
                                  for role in COMPARISON_FIELD_IDS}
