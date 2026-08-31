from review_writer_core.publication_scope import (
    methods_execution_report,
    public_scope_statement,
)


def test_local_scope_never_turns_workflow_time_into_search_cutoff() -> None:
    text = public_scope_statement(
        search_record={
            "retrieved_at": "2026-08-28T12:00:00Z",
            "selected_matrix_candidate_count": 16,
            "successful_sources": [],
        },
        coverage_diagnostics={"coverage_mode": "local_bounded"},
        scope_contract={},
    )

    assert "assembled local source corpus" in text
    assert "2026-08-28" not in text
    assert "through" not in text.casefold()
    assert "16 sources" in text


def test_successful_external_search_reports_execution_date_not_coverage_cutoff() -> None:
    text = public_scope_statement(
        search_record={
            "retrieved_at": "2026-08-28T12:00:00Z",
            "successful_sources": ["crossref", "openalex"],
        },
        coverage_diagnostics={"coverage_mode": "multi_source"},
        scope_contract={},
    )

    assert "Crossref, OpenAlex" in text
    assert "run on 2026-08-28" in text
    assert "through 2026-08-28" not in text


def test_methods_execution_reports_partial_sources_without_blocking() -> None:
    report = methods_execution_report(
        {
            "requested_sources": ["crossref", "openalex"],
            "executed_sources": ["crossref", "openalex"],
            "successful_sources": ["crossref"],
            "failed_sources": ["openalex"],
        }
    )

    assert report["status"] == "external_partial"
    assert report["issues"] == [
        {"type": "external_sources_failed", "sources": ["openalex"]}
    ]


def test_local_bounded_limitation_survives_full_scope_sentence_set() -> None:
    text = public_scope_statement(
        search_record={
            "successful_sources": ["crossref"],
            "retrieved_at": "2026-08-28T12:00:00Z",
            "selected_matrix_candidate_count": 16,
            "coverage_mode": "local_bounded",
        },
        scope_contract={"time_span": {"from": 1979, "to": 2021}},
    )

    assert "does not claim exhaustive global coverage" in text
