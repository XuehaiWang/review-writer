from review_writer_api.domain_services.library import LibraryPaperRecord
from review_writer_api.routers.library import _paper_payload


def paper_record(metadata, *, parsed=True):
    return LibraryPaperRecord(
        id="id", paper_id="P001", title="A paper", authors=["A. Author"],
        keywords=[], tags={}, original_filename="paper.pdf", content_sha256="hash",
        metadata=metadata, bibliography_audit={}, pdf_relative_path="paper.pdf",
        markdown_relative_path="paper.md",
        artifact_ids={"pdf": "pdf-id", "markdown": "md-id", **({"mineru": "mineru-id"} if parsed else {})},
        updated_at="2026-09-25T00:00:00Z",
    )


def complete_metadata():
    return {
        "title": {"value": "A paper"}, "authors": {"value": ["A. Author"]},
        "year": {"value": 2026}, "journal": {"value": "Journal"},
        "abstract": {"value": "A full abstract."}, "doi": {"value": ""},
        "human_review": {"status": "not_reviewed"},
        "quality": {"needs_human_check": True, "warnings": ["missing_doi", "low_confidence_abstract"]},
    }


def test_complete_unreviewed_paper_is_green_without_doi():
    result = _paper_payload(paper_record(complete_metadata()))
    assert result["content_complete"] is True
    assert result["content_missing_fields"] == []
    assert result["needs_human_check"] is True  # Review remains a separate concern.


def test_reviewed_paper_with_missing_content_stays_incomplete():
    metadata = complete_metadata()
    metadata["human_review"] = {"status": "reviewed"}
    metadata["authors"] = {"value": [" "]}
    metadata["abstract"] = {"value": ""}
    result = _paper_payload(paper_record(metadata))
    assert result["content_complete"] is False
    assert result["content_missing_fields"] == ["authors", "abstract"]


def test_unparsed_body_is_incomplete_even_with_all_metadata():
    result = _paper_payload(paper_record(complete_metadata(), parsed=False))
    assert result["content_complete"] is False
    assert result["content_missing_fields"] == ["fulltext"]
