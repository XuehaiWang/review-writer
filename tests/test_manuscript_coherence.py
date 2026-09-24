import pytest

from review_writer_core.manuscript_coherence import (
    manuscript_snapshot, plan_manuscript, audit_candidate, candidate_check_current,
)


def rows():
    return [{"paragraph_id": "S1-p1", "paragraph_key": "a", "text": "Only under these conditions."},
            {"paragraph_id": "S2-p1", "paragraph_key": "b", "text": "Results differ across substrates."}]


def test_plan_requires_exact_location_and_known_dependencies():
    snapshot = manuscript_snapshot(rows())
    issue = {"paragraph_id": "S1-p1", "excerpt": "Only under these conditions.",
             "instruction": "Keep scope explicit.", "dependencies": ["S2-p1"]}
    assert plan_manuscript(snapshot, lambda *a, **k: {"issues": [issue]})["status"] == "complete"
    for change in ({"excerpt": "invented"}, {"dependencies": ["unknown"]}, {"paragraph_id": "unknown"}):
        with pytest.raises(ValueError):
            plan_manuscript(snapshot, lambda *a, **k: {"issues": [{**issue, **change}]})


def test_long_manuscript_plan_cannot_claim_full_coverage():
    snapshot = manuscript_snapshot(rows(), limit=30)
    assert len(snapshot["paragraphs"]) == 1
    assert plan_manuscript(snapshot, lambda *a, **k: {"issues": []})["status"] == "partial"


def test_correct_ref_does_not_override_semantic_failure_and_checks_are_text_bound():
    sources = [{"ref": "A:1", "text": "Only under these conditions."}]
    for response in ({"status": "unsupported", "preserves_information": True, "source_refs": ["A:1"]},
                     {"status": "supported", "preserves_information": False, "source_refs": ["A:1"]},
                     {"status": "supported", "preserves_information": True, "source_refs": ["fake"]}, {}):
        assert audit_candidate("Only under these conditions.", "Always works.", sources,
                               lambda *a, **k: response)["status"] == "unresolved"
    candidate = "Under these conditions only."
    check = audit_candidate("Only under these conditions.", candidate, sources,
                            lambda *a, **k: {"status": "supported", "preserves_information": True, "source_refs": ["A:1"]})
    saved = {"candidate_text": candidate, "sources": sources, "automatic_source_check": check}
    assert candidate_check_current(saved)
    assert not candidate_check_current({**saved, "candidate_text": "Always works."})
    assert not candidate_check_current({**saved, "sources": [{"ref": "A:1", "text": "Changed source"}]})
