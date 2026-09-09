"""Internal search execution provenance; never injected into narrative prose."""
from __future__ import annotations

from typing import Any, Iterable


def _unique_text(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            text
            for value in values
            if (text := " ".join(str(value or "").split()).strip())
        )
    )


def methods_execution_report(
    search_record: dict[str, Any],
) -> dict[str, Any]:
    """Describe the internal execution result without creating a release gate."""

    requested = _unique_text(search_record.get("requested_sources") or [])
    executed = _unique_text(search_record.get("executed_sources") or [])
    successful = _unique_text(search_record.get("successful_sources") or [])
    failed = _unique_text(search_record.get("failed_sources") or [])
    not_executed = [source for source in requested if source not in executed]
    issues: list[dict[str, Any]] = []
    if failed:
        issues.append(
            {"type": "external_sources_failed", "sources": failed}
        )
    if not_executed:
        issues.append(
            {"type": "requested_sources_not_executed", "sources": not_executed}
        )
    status = (
        "external_partial"
        if successful and (failed or not_executed)
        else "external_recorded"
        if successful
        else "local_bounded"
    )
    return {
        "status": status,
        "requested_sources": requested,
        "executed_sources": executed,
        "successful_sources": successful,
        "failed_sources": failed,
        "publication_methods_section": "omitted",
        "publication_scope_note": "internal_only",
        "selected_source_count": search_record.get("selected_matrix_candidate_count") or 0,
        "retrieved_at": search_record.get("retrieved_at") or "",
        "issues": issues,
    }
