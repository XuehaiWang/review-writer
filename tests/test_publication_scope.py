from review_writer_core.publication_scope import methods_execution_report


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


def test_local_scope_is_retained_as_internal_data_without_prose():
    report = methods_execution_report({"selected_matrix_candidate_count": 16,
                                       "retrieved_at": "2026-08-28T12:00:00Z"})
    assert report["publication_scope_note"] == "internal_only"
    assert report["selected_source_count"] == 16
    assert report["retrieved_at"] == "2026-08-28T12:00:00Z"
    assert report["status"] == "local_bounded"
