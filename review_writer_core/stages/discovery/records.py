"""Pure Discovery coverage and reproducibility records."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from review_writer_core.metadata_fields import unwrap_metadata_value


def _candidate_id(row: dict[str, Any], *, external: bool) -> str:
    if external:
        return str(
            row.get("candidate_id")
            or row.get("doi")
            or row.get("url")
            or f"{row.get('title', '')}|{row.get('year', '')}"
        ).strip()
    return str(row.get("paper_id") or "").strip()


def _selected(row: dict[str, Any]) -> bool:
    return bool(row.get("selected_for_matrix")) and str(row.get("role") or "") != "excluded"


def publication_year(value: Any) -> int | None:
    raw = unwrap_metadata_value(value)
    match = re.search(r"(?:18|19|20|21)\d{2}", str(raw or ""))
    return int(match.group(0)) if match else None


def discovery_coverage_diagnostics(review: dict[str, Any]) -> dict[str, Any]:
    """Describe observable local-result gaps without claiming global recall."""

    unique: dict[str, dict[str, Any]] = {}
    empty_groups: list[str] = []
    for group in review.get("results") or []:
        if not isinstance(group, dict) or group.get("keep") is False:
            continue
        local = [row for row in group.get("local_results") or [] if isinstance(row, dict)]
        if not local:
            empty_groups.append(str(group.get("keyword") or "").strip())
        for row in local:
            paper_id = _candidate_id(row, external=False)
            if paper_id:
                unique.setdefault(paper_id, row)
    years = [
        year
        for row in unique.values()
        if (year := publication_year(row.get("first_publication_date") or row.get("year")))
        is not None
    ]
    year_distribution = Counter(str(year) for year in years)
    query_plan = review.get("query_plan") if isinstance(review.get("query_plan"), dict) else {}
    filters = review.get("filters") if isinstance(review.get("filters"), dict) else {}
    if not filters and isinstance(query_plan.get("filters"), dict):
        filters = query_plan["filters"]
    year_from = publication_year(filters.get("year_from"))
    year_to = publication_year(filters.get("year_to"))
    if year_from is None or year_to is None:
        topic_range = re.search(
            r"(?<!\d)((?:18|19|20|21)\d{2})\s*(?:-|–|—|to|至)\s*((?:18|19|20|21)\d{2})(?!\d)",
            str(review.get("topic") or ""),
            re.I,
        )
        if topic_range:
            year_from = int(topic_range.group(1))
            year_to = int(topic_range.group(2))
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from
    missing_years: list[int] = []
    if year_from is not None and year_to is not None and year_to - year_from <= 40:
        observed = set(years)
        missing_years = [year for year in range(year_from, year_to + 1) if year not in observed]
    external = review.get("external_search") if isinstance(review.get("external_search"), dict) else {}
    requested_sources = [
        str(value)
        for value in external.get("requested_sources") or []
        if str(value).strip()
    ]
    source_statuses = (
        external.get("source_statuses")
        if isinstance(external.get("source_statuses"), dict)
        else {}
    )
    completed_sources = [
        str(name)
        for name, status in source_statuses.items()
        if isinstance(status, dict) and str(status.get("status") or "") == "completed"
    ]
    for value in external.get("sources_used") or []:
        source = str(value).strip()
        if source and source not in completed_sources:
            completed_sources.append(source)
    reason_codes: list[str] = []
    if len(unique) < 10:
        reason_codes.append("coverage.local_candidate_pool_small")
    unknown_year_count = len(unique) - len(years)
    if unknown_year_count >= max(2, (len(unique) + 3) // 4):
        reason_codes.append("coverage.publication_years_unknown")
    span = (year_to - year_from + 1) if year_from is not None and year_to is not None else 0
    if span and len(missing_years) >= max(2, (span + 2) // 3):
        reason_codes.append("coverage.explicit_year_range_sparse")
    if empty_groups:
        reason_codes.append("coverage.query_groups_empty")
    completion_state = str(external.get("completion_state") or "disabled")
    online_search_suggested = bool(
        reason_codes
        and (
            not completed_sources
            or completion_state in {"disabled", "failed", "partial"}
        )
    )
    return {
        "schema_version": 1,
        "coverage_mode": "multi_source" if completed_sources else "local_bounded",
        "coverage_claim": "selected_corpus_only",
        "candidate_paper_count": len(unique),
        "year_distribution": dict(sorted(year_distribution.items())),
        "year_unknown_count": unknown_year_count,
        "declared_year_from": year_from,
        "declared_year_to": year_to,
        "missing_years": missing_years,
        "empty_query_groups": [value for value in empty_groups if value],
        "requested_online_sources": requested_sources,
        "completed_online_sources": completed_sources,
        "online_search_completion_state": completion_state,
        "online_search_suggested": online_search_suggested,
        "reason_codes": reason_codes,
        "limitations": [
            "The diagnosis describes only the observed local results and configured query plan.",
            "It does not estimate an unknowable global recall percentage.",
        ],
    }


def discovery_search_record(review: dict[str, Any]) -> dict[str, Any]:
    """Build a reproducible record from observed Discovery execution facts."""

    groups = [
        group
        for group in review.get("results") or []
        if isinstance(group, dict) and group.get("keep") is not False
    ]
    local_rows = [
        row
        for group in groups
        for row in group.get("local_results") or []
        if isinstance(row, dict)
    ]
    external_rows = [
        row
        for group in groups
        for row in group.get("web_results") or []
        if isinstance(row, dict)
    ]
    external = review.get("external_search") if isinstance(review.get("external_search"), dict) else {}
    statuses = external.get("source_statuses") if isinstance(external.get("source_statuses"), dict) else {}
    executed_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and (
            int(row.get("completed_queries") or 0) > 0
            or str(row.get("status") or "") == "completed"
        )
    ]
    for source in external.get("executed_sources") or []:
        normalized = str(source).strip()
        if normalized and normalized not in executed_sources:
            executed_sources.append(normalized)
    successful_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict) and str(row.get("status") or "") == "completed"
    ]
    for source in external.get("successful_sources") or []:
        normalized = str(source).strip()
        if normalized and normalized not in successful_sources:
            successful_sources.append(normalized)
    enabled_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and str(row.get("status") or "") not in {"disabled", "not_configured"}
    ]
    failed_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and str(row.get("status") or "") in {"failed", "error", "partial"}
    ]
    query_log = [dict(row) for row in external.get("query_log") or [] if isinstance(row, dict)]
    if not query_log:
        query_log = [
            {
                "query_group": str(group.get("keyword") or "").strip(),
                "query": str(group.get("query") or group.get("keyword") or "").strip(),
                "status": "record_recovered_from_saved_results",
            }
            for group in groups
            if str(group.get("keyword") or "").strip()
        ]
    local_ids = {
        _candidate_id(row, external=False)
        for row in local_rows
        if _candidate_id(row, external=False)
    }
    external_ids = {
        _candidate_id(row, external=True)
        for row in external_rows
        if _candidate_id(row, external=True)
    }
    selected_ids = {
        _candidate_id(row, external=False) for row in local_rows if _selected(row)
    }
    return {
        "schema_version": 1,
        "retrieved_at": str(
            external.get("completed_at")
            or review.get("searched_at")
            or review.get("generated_at")
            or review.get("updated_at")
            or ""
        ),
        "requested_sources": [
            str(value)
            for value in external.get("requested_sources") or []
            if str(value).strip()
        ],
        "enabled_sources": enabled_sources,
        "executed_sources": executed_sources,
        "successful_sources": successful_sources,
        "failed_sources": failed_sources,
        "contributing_sources": [
            str(value)
            for value in external.get("sources_used") or []
            if str(value).strip()
        ],
        "source_statuses": deepcopy(statuses),
        "completion_state": str(external.get("completion_state") or "disabled"),
        "query_log": query_log,
        "initial_local_hit_count": len(local_rows),
        "unique_local_candidate_count": len(local_ids),
        "initial_external_hit_count": len(external_rows),
        "unique_external_candidate_count": len(external_ids),
        "selected_matrix_candidate_count": len(selected_ids),
        "explicitly_excluded_local_count": len(
            {
                _candidate_id(row, external=False)
                for row in local_rows
                if str(row.get("role") or "") == "excluded"
            }
        ),
        "deduplication_basis": "stable local Paper ID; external DOI/URL/source identity",
        "citation_tracking": "not_performed",
        "structure_search": "not_performed",
    }
