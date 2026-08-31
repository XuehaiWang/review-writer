"""Target-level issue aggregation for the existing Final validation payload."""

from __future__ import annotations

from typing import Any


def final_issue_details(validation: dict[str, Any]) -> list[dict[str, Any]]:
    """Project existing findings into actionable rows without new quality state."""

    rows: list[dict[str, Any]] = []

    def add(target_type: str, target_id: object, issues: object) -> None:
        normalized = list(
            dict.fromkeys(
                str(issue).strip()
                for issue in (issues if isinstance(issues, list) else [issues])
                if str(issue or "").strip()
            )
        )
        if normalized:
            rows.append(
                {
                    "target_type": target_type,
                    "target_id": str(target_id or target_type).strip() or target_type,
                    "issues": normalized,
                }
            )

    for finding in validation.get("figure_argument_findings") or []:
        if isinstance(finding, dict):
            add("figure", finding.get("figure_id"), finding.get("issues") or [])
    for finding in (validation.get("claim_citation_mapping") or {}).get("issues") or []:
        if isinstance(finding, dict):
            add("claim", finding.get("claim_id"), finding.get("issues") or [])
    for paper in (validation.get("bibliography_identity") or {}).get("papers") or []:
        if not isinstance(paper, dict) or paper.get("verified"):
            continue
        issues = [
            *list(paper.get("missing_fields") or []),
            *list(paper.get("polluted_fields") or []),
            *list(paper.get("unresolved_conflicts") or []),
        ]
        add(
            "reference",
            paper.get("paper_id"),
            issues or ["bibliography_identity_unresolved"],
        )
    overview = validation.get("overview_semantics") or {}
    add("overview", "review_overview", overview.get("issues") or [])
    classification = validation.get("classification_contract") or {}
    add(
        "outline",
        "classification_contract",
        [
            *list(classification.get("missing_topic_partitions") or []),
            *(
                ["classification_contract_drift"]
                if str(classification.get("status") or "") == "drift"
                else []
            ),
        ],
    )
    for paper_id in validation.get("metadata_changed_after_blueprint_paper_ids") or []:
        add("reference", paper_id, ["metadata_changed_after_blueprint"])
    for paragraph_id in validation.get("unverified_manual_paragraph_ids") or []:
        add("paragraph", paragraph_id, ["unverified_manual_claims_exported"])

    covered = {issue for row in rows for issue in row.get("issues") or []}
    target_types = {str(row.get("target_type") or "") for row in rows}
    if "figure" in target_types:
        covered.update(
            {"figure_evidence_binding_incomplete", "figure_rights_unresolved"}
        )
    if "claim" in target_types:
        covered.update(
            {"claim_citation_mapping_failure", "citation_identity_unresolved"}
        )
    if "reference" in target_types:
        covered.update(
            {
                "bibliography_identity_unresolved",
                "scope_selection_requires_recheck_after_metadata_change",
            }
        )
    if "overview" in target_types:
        covered.add("overview_semantics_invalid")
    if "outline" in target_types:
        covered.add("classification_contract_drift")
    if "paragraph" in target_types:
        covered.add("unverified_manual_claims_exported")
    for issue in [
        *list(validation.get("blocking_issues") or []),
        *list(validation.get("warning_issues") or []),
        *list(validation.get("release_integrity_issues") or []),
    ]:
        normalized = str(issue or "").strip()
        if normalized and normalized not in covered:
            add("manuscript", "current_final", [normalized])
            covered.add(normalized)
    return rows
