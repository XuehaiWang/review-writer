from review_writer_core.paragraph_markers import ensure_prose_paragraph_markers


def test_deleted_identity_is_not_assigned_to_new_prose():
    source = "## Test\n\nOld prose.\n\n<!-- paragraph_id: S01-p1 -->\n\nNew prose.\n"
    first, report = ensure_prose_paragraph_markers(source)
    retired = report["inserted"][0]
    deleted = first.replace(f"New prose.\n\n<!-- paragraph_id: {retired} -->", f"<!-- deleted_paragraph_id: {retired} -->")
    result, report = ensure_prose_paragraph_markers(deleted + "\nAnother paragraph.\n")
    assert retired not in report["inserted"]
    assert "<!-- paragraph_id: S01-p1 -->" in result
