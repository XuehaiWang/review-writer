"""Coverage check is advisory: it reports gaps but never blocks."""

from review_writer_core.section_paragraph_coverage import validate_paragraph_coverage


def _paragraph(refs, records, kind="reported_result"):
    return {
        "paragraph_id": "S1-p1",
        "claim_realizations": [{
            "claim_id": "c1",
            "claim_kind": kind,
            "evidence_refs": [{"evidence_key": f"E{i}", "paper_id": p} for i, p in enumerate(refs)],
            "result_context": records,
        }],
    }


def test_complete_paragraph_has_no_issues():
    paragraph = _paragraph(["P1", "P2"], [
        {"object": "terminal alkynes", "result": "80% yield"},
        {"object": "aryl aldehydes", "result": "70% yield"},
    ])
    assert validate_paragraph_coverage(paragraph, primary_papers=["P1", "P2"]) == []


def test_missing_primary_paper_is_reported():
    paragraph = _paragraph(["P1"], [{"object": "a", "result": "80%"}])
    assert "primary_paper_not_covered" in validate_paragraph_coverage(
        paragraph, primary_papers=["P1", "P2"])


def test_missing_transformation_or_outcome_is_reported():
    no_object = _paragraph(["P1"], [{"object": "", "result": "80% yield"}])
    assert "transformation_not_named" in validate_paragraph_coverage(no_object)
    no_outcome = _paragraph(["P1"], [{"object": "a", "result": ""}])
    assert "outcome_not_named" in validate_paragraph_coverage(no_outcome)


def test_paragraph_without_any_claim_is_reported():
    assert validate_paragraph_coverage({"paragraph_id": "p"}) == ["no_bound_claim"]
