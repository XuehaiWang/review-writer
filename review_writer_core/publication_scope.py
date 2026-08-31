"""Reader-facing review-scope prose derived only from observed execution facts.

The workflow timestamp records when a search job ran.  It is not a claim that
the literature was covered *through* that date.  Keeping this renderer in the
core prevents API and export paths from inventing different search methods.
"""

from __future__ import annotations

from typing import Any, Iterable


PUBLIC_SOURCE_NAMES = {
    "crossref": "Crossref",
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "arxiv": "arXiv",
}


def _unique_text(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            text
            for value in values
            if (text := " ".join(str(value or "").split()).strip())
        )
    )


def _source_names(values: Iterable[Any]) -> list[str]:
    return [
        PUBLIC_SOURCE_NAMES.get(
            value.casefold(), value.replace("_", " ").title()
        )
        for value in _unique_text(values)
    ]


def public_scope_statement(
    *,
    search_record: dict[str, Any],
    coverage_diagnostics: dict[str, Any] | None = None,
    scope_contract: dict[str, Any] | None = None,
) -> str:
    """Render concise Introduction prose without overstating search coverage."""

    coverage = dict(coverage_diagnostics or {})
    scope = dict(scope_contract or {})
    successful = _source_names(search_record.get("successful_sources") or [])
    sentences: list[str] = []
    if successful:
        sentences.append(
            "Literature searches were executed using "
            + ", ".join(successful)
            + "."
        )
        executed_on = str(search_record.get("retrieved_at") or "").strip()[:10]
        if executed_on:
            # "on" describes the actual operation; "through" would assert an
            # unsupported publication-coverage boundary.
            sentences.append(f"The recorded searches were run on {executed_on}.")
    else:
        sentences.append(
            "This selective narrative review is based on the assembled local source corpus."
        )

    selected_count = int(search_record.get("selected_matrix_candidate_count") or 0)
    if selected_count:
        sentences.append(
            "After publication-identity deduplication and relevance screening, "
            f"{selected_count} sources were retained for evidence synthesis."
        )

    time_span = scope.get("time_span") if isinstance(scope.get("time_span"), dict) else {}
    year_from = str(time_span.get("from") or "").strip()
    year_to = str(time_span.get("to") or "").strip()
    if year_from and year_to:
        sentences.append(
            f"The main synthesis focuses on publications from {year_from} to {year_to}; "
            "earlier retained sources provide historical context."
        )

    coverage_mode = str(
        coverage.get("coverage_mode") or search_record.get("coverage_mode") or ""
    ).strip()
    if coverage_mode == "local_bounded" and not any(
        "selective narrative" in sentence.casefold() for sentence in sentences
    ):
        sentences.append(
            "The discussion is therefore selective and does not claim exhaustive global coverage."
        )
    # Do not truncate the final scope limitation. In partial-search cases it
    # commonly follows provider, execution-date, retained-count, and year-span
    # facts; dropping it would overstate the review's coverage.
    return " ".join(sentences)


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
        "publication_scope_note": "included_in_introduction",
        "issues": issues,
    }
