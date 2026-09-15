from review_writer_core.bibliography_audit import refresh_edited_bibliography


def test_ordinary_save_does_not_claim_source_verification():
    before = {"title": "Original", "journal": "Journal"}
    after = {"title": "Corrected", "journal": ""}
    result = refresh_edited_bibliography(before, after, {"status": "verified", "manual_review_status": "approved"})
    assert result["status"] == "not_audited"
    assert result["manual_review_status"] == "not_reviewed"
    assert "journal" in result["automatic_resolution_missing_fields"]


def test_non_bibliographic_edit_preserves_verification():
    result = refresh_edited_bibliography({"title": "A"}, {"title": "A", "notes": "Updated"}, {"status": "verified"})
    assert result["status"] == "verified"
