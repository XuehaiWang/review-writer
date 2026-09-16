import pytest

from review_writer_core.bibliography_audit import bibliography_field_readiness


@pytest.mark.parametrize("extra", [{}, {"doi": "10.1000/example"}, {"publication_status": "online_first"}])
def test_missing_pagination_is_optional_with_or_without_doi(extra):
    metadata = {"title": "A study of catalytic methods", "authors": ["Alice Smith"],
                "journal": "Journal of Chemistry", "year": 2024, **extra}
    result = bibliography_field_readiness(metadata, {"status": "verified"})
    assert result["ready"]
    assert result["missing_fields"] == []
    assert result["optional_missing_fields"] == ["pages_or_article_number"]
    assert "pages" not in metadata and "article_number" not in metadata


@pytest.mark.parametrize("field", ["pages", "article_number", "locator"])
def test_existing_locator_remains_available_and_needs_no_completion(field):
    result = bibliography_field_readiness({"title": "A study", "authors": ["Alice Smith"],
        "journal": "Journal of Chemistry", "year": 2024, field: "123"})
    assert result["ready"]
    assert result["optional_missing_fields"] == []


def test_required_identity_fields_and_conflicts_are_still_checked():
    result = bibliography_field_readiness({"title": "A study", "authors": ["Alice Smith"],
        "year": 2024}, {"unresolved_conflicts": [{"field": "title"}]})
    assert not result["ready"]
    assert "journal" in result["missing_fields"]
    assert result["unresolved_conflicts"]
