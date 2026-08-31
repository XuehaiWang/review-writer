"""Deterministic repair routing for Draft quality findings.

The router deliberately does not execute repairs.  It identifies the earliest
workflow owner and whether a paragraph rewrite is scientifically safe.  API
services can then keep using their existing targeted endpoints instead of
growing a second, generic repair service.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .review_fact_readiness import evidence_problem_type as classify_evidence_problem


def _contains(text: str, terms: set[str]) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
        for term in terms
    )


DISCOVERY_TERMS = {
    "search",
    "retrieval",
    "coverage",
    "corpus",
    "missing_primary",
    "recall",
    "sampling",
    "publication_bias",
}
METADATA_TERMS = {
    "metadata conflict",
    "bibliographic identity",
    "paper identity",
    "doi conflict",
    "year conflict",
    "author conflict",
    "journal conflict",
    "fact conflict",
}
PLANNING_TERMS = {
    "taxonomy",
    "classification",
    "matrix",
    "outline",
    "organization",
    "section_structure",
    "category",
    "single-paper",
    "catch-all",
}
SYNTHESIS_TERMS = {
    "comparison coverage",
    "comparison insufficient",
    "insufficient comparison",
    "comparison_axis",
    "required_cross_study_comparison_missing",
    "section exit",
    "synthesis exit",
    "section_synthesis_exit_position",
    "cross_study_comparison_quota",
}
EVIDENCE_TERMS = {
    "required_claim",
    "no supporting evidence",
    "missing supporting evidence",
    "unsupported claim",
    "source unavailable",
    "source passage missing",
    "local source unavailable",
    "claim evidence missing",
    "evidence gap",
    "c01",
}
FIGURE_TERMS = {
    "figure callout",
    "scheme callout",
    "visible_callout",
    "figure insertion",
    "figure placement",
    "image placement",
    "figure argument",
    "caption mismatch",
}
BIBLIOGRAPHY_TERMS = {
    "bibliography",
    "reference field",
    "reference metadata",
    "missing journal",
    "missing pages",
    "article number",
    "doi_or_locator",
}
FINAL_TERMS = {
    "export",
    "docx",
    "pdf",
    "xml incompatible",
    "unresolved placeholder",
    "unsupported markup",
}


def _matched_terms(text: str, terms: set[str]) -> list[str]:
    return sorted(term for term in terms if _contains(text, {term}))


def issue_fingerprint(issue: dict[str, Any], repair: dict[str, Any]) -> str:
    """Return a wording-insensitive identity for one repair target.

    Evaluator prose is free-form and can change between runs.  A fingerprint
    therefore uses the paragraph, executable route, rule/dimension identities,
    and only recognized deterministic signal families.  It intentionally does
    not hash the diagnosis sentence itself.
    """

    searchable = " ".join(
        [
            str(issue.get("issue_type") or ""),
            str(issue.get("rule_id") or issue.get("rule") or ""),
            str(issue.get("diagnosis") or issue.get("message") or ""),
            *[str(value) for value in issue.get("failed_dimensions") or []],
        ]
    ).casefold()
    signals = _matched_terms(
        searchable,
        DISCOVERY_TERMS
        | METADATA_TERMS
        | PLANNING_TERMS
        | SYNTHESIS_TERMS
        | EVIDENCE_TERMS
        | FIGURE_TERMS
        | BIBLIOGRAPHY_TERMS
        | FINAL_TERMS,
    )
    dimensions = sorted(
        {
            str(value).strip().casefold()
            for value in issue.get("failed_dimensions") or []
            if str(value).strip()
        }
    )
    if dimensions:
        # Rubric/rule identifiers are the stronger stable identity. Diagnosis
        # wording and matched phrases may vary between provider calls.
        signals = []
    key = "|".join(
        [
            str(issue.get("paragraph_id") or "global"),
            str(repair.get("repair_route") or "paragraph_rewrite"),
            str(repair.get("issue_type") or "draft_wording"),
            ",".join(dimensions),
            ",".join(signals),
        ]
    )
    return "ISSUE-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper()


def route_draft_issue(
    issue: dict[str, Any],
    *,
    source_status: str = "not_assessed",
    evaluator_route: str = "",
    has_original_passages: bool = False,
    reference_map_problem: bool = False,
    source_evidence_refs: list[Any] | None = None,
    source_ready: bool = False,
    evidence_texts: list[Any] | None = None,
) -> dict[str, Any]:
    """Return stable repair metadata for one quality issue.

    ``repair_route`` remains the executable action name used by the existing
    Draft optimizer.  ``repair_stage`` and ``repair_target`` expose the actual
    workflow owner so the UI can send users to a precise target when automatic
    repair is not appropriate.
    """

    source_status = str(source_status or "not_assessed").casefold()
    evaluator_route = str(evaluator_route or "").casefold()
    failed_dimensions = [
        str(value)
        for value in issue.get("failed_dimensions") or []
        if str(value).strip()
    ]
    # Persisted quality rows can already contain the router's former derived
    # metadata. Never feed that output back as fresh evidence for the next
    # routing decision, or an old mistake becomes self-perpetuating.
    issue_type_signal = (
        ""
        if issue.get("repair_stage") or issue.get("repair_route")
        else str(issue.get("issue_type") or "")
    )
    searchable = " ".join(
        [
            evaluator_route,
            source_status,
            *failed_dimensions,
            str(issue.get("diagnosis") or issue.get("message") or ""),
            issue_type_signal,
            str(issue.get("rule_id") or ""),
        ]
    ).casefold()

    result: dict[str, Any] = {
        "repair_routing_version": 2,
        "issue_type": "draft_wording",
        "repair_route": "paragraph_rewrite",
        "repair_stage": "draft",
        "repair_action": "rewrite_paragraph",
        "repair_target": {
            "stage": "draft",
            "paragraph_id": str(issue.get("paragraph_id") or ""),
        },
        "auto_repairable": True,
        "rewrite_eligible": True,
        "internal_repair_stage": "draft",
        "recommended_action": (
            "Revise this paragraph without changing supported scientific claims."
        ),
        "evidence_problem_type": str(issue.get("evidence_problem_type") or "")
        or classify_evidence_problem(
            unsupported_claims=issue.get("unsupported_claims") or [],
            source_check_status=source_status,
            source_evidence_refs=source_evidence_refs or [],
            source_ready=source_ready,
            evidence_texts=evidence_texts or [],
        ),
    }

    if _contains(searchable, FIGURE_TERMS):
        result.update(
            issue_type="figure_argument_or_placement",
            repair_route="figure_insertion_repair",
            repair_stage="figures",
            repair_action="rebuild_figure_insertion_plan",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="figures",
            recommended_action=(
                "Rebuild the current figure insertion decision, callout, and caption."
            ),
        )
    elif _contains(searchable, BIBLIOGRAPHY_TERMS):
        result.update(
            issue_type="bibliography_metadata",
            repair_route="bibliography_repair",
            repair_stage="bibliography",
            repair_action="repair_canonical_bibliography",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="bibliography",
            recommended_action=(
                "Repair the canonical bibliography record; do not rewrite prose to hide it."
            ),
        )
    elif _contains(searchable, FINAL_TERMS):
        result.update(
            issue_type="final_export_integrity",
            repair_route="final_export_repair",
            repair_stage="final",
            repair_action="rebuild_final_export",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="final",
            recommended_action="Rebuild the current Final artifact and export checks.",
        )
    elif _contains(searchable, SYNTHESIS_TERMS):
        result.update(
            issue_type="synthesis_plan_gap",
            repair_route="synthesis_plan_repair",
            repair_stage="writing_plan",
            repair_action="rebuild_section_synthesis_and_writing_plan",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="sections",
            recommended_action=(
                "Rebuild the affected section synthesis state and writing plan."
            ),
        )
    elif _contains(searchable, DISCOVERY_TERMS):
        result.update(
            issue_type="literature_coverage_gap",
            repair_route="manual_online_retrieval_decision",
            repair_stage="discovery",
            repair_action="broaden_or_correct_retrieval",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="discovery",
            recommended_action=(
                "Broaden or correct the retrieval scope, then refresh Matrix evidence."
            ),
        )
    elif _contains(searchable, METADATA_TERMS):
        result.update(
            issue_type="metadata_or_fact_conflict",
            repair_route="metadata_matrix_repair",
            repair_stage="library_matrix",
            repair_action="recheck_local_metadata_and_matrix_fact",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="matrix",
            recommended_action=(
                "Recheck the local publication metadata and Matrix fact before rewriting."
            ),
        )
    elif _contains(searchable, PLANNING_TERMS):
        result.update(
            issue_type="planning_structure",
            repair_route="planning_revision",
            repair_stage="planning",
            repair_action="revise_matrix_or_outline",
            auto_repairable=False,
            rewrite_eligible=False,
            internal_repair_stage="planning",
            recommended_action=(
                "Correct the Matrix classification or section structure before rewriting."
            ),
        )
    elif reference_map_problem and source_status in {
        "verified",
        "not_applicable",
        "not_assessed",
    }:
        result.update(
            issue_type="citation_reference_mapping",
            repair_route="deterministic_reference_rebuild",
            repair_stage="bibliography",
            repair_action="rebuild_citation_reference_map",
            auto_repairable=True,
            rewrite_eligible=False,
            internal_repair_stage="draft",
            recommended_action=(
                "Rebuild the citation and reference map deterministically."
            ),
        )
    elif (
        source_status in {"partially_supported", "unsupported", "needs_human_review"}
        or evaluator_route == "local_source_recheck"
        or _contains(searchable, EVIDENCE_TERMS)
    ):
        repair_route = (
            "targeted_evidence_then_paragraph_rewrite"
            if has_original_passages
            else "claim_downgrade_then_paragraph_rewrite"
        )
        result.update(
            issue_type="claim_evidence_gap",
            repair_route=repair_route,
            repair_stage="evidence_package",
            repair_action=(
                "attach_local_evidence_then_rewrite"
                if has_original_passages
                else "downgrade_unsupported_claim_then_rewrite"
            ),
            auto_repairable=source_status != "needs_human_review",
            rewrite_eligible=source_status != "needs_human_review",
            internal_repair_stage="sections",
            recommended_action=(
                "Attach matching local-source passages and rewrite only this paragraph."
                if has_original_passages
                else "Keep the Claim trace, lower unsupported detail, and rewrite only this paragraph."
            ),
        )

    result["repair_target"] = {
        "stage": result["repair_stage"],
        "paragraph_id": str(issue.get("paragraph_id") or ""),
        "section_id": str(issue.get("section_id") or ""),
        "paper_ids": [
            str(value)
            for value in issue.get("paper_ids") or []
            if str(value).strip()
        ],
    }
    result["issue_fingerprint"] = issue_fingerprint(issue, result)
    return result
