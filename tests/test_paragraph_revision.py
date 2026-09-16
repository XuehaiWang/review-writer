import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from review_writer_core.paragraph_revision import validate_revision_response, paragraph_keys


def test_unknown_source_reference_is_not_accepted():
    with pytest.raises(ValueError, match="unknown source"):
        validate_revision_response({"reply": "Reason", "candidate_text": "Text", "source_refs": ["invented"]},
            {"evidence": [{"original_passages": [{"ref": "P1:b2"}]}]})


def test_stable_keys_follow_saved_identity_not_position():
    first = "One.\n<!-- paragraph_id: S1-p1 -->\n\nTwo.\n<!-- paragraph_id: S1-p2 -->\n"
    keys = paragraph_keys(first, {}, "draft1")
    assert keys
    moved = "Two.\n<!-- paragraph_id: S1-p2 -->\n\nOne.\n<!-- paragraph_id: S1-p1 -->\n"
    assert paragraph_keys(moved, {"paragraph_keys": keys}, "draft2") == keys


@pytest.mark.parametrize("human", [False, True])
def test_chapter_author_can_remove_uppercase_junk_but_batch_remains_strict(tmp_path, monkeypatch, human):
    scripts = Path(__file__).resolve().parents[1] / "skills/review-first-draft-feedback-loop/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("author_revision_test", scripts / "revise_paragraph.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = "LCHT Allenes are useful. [1]"
    candidate = "Allenes are useful. [1]"
    first = tmp_path / "04_first_draft"
    first.mkdir()
    (first / "first_draft.md").write_text(original, encoding="utf-8")
    request = {"paragraph_id": "S1-p1", "discussion_text": original, "message": "Remove the stray prefix",
               "routing": {"mode": "revision"}}
    if human:
        request["section_context"] = {"title": "Introduction"}
    with patch.object(module.loop, "parse_marked_paragraphs", return_value=[{"paragraph_id": "S1-p1", "text": original}]), \
         patch.object(module.loop, "matrix_rows", return_value={}), \
         patch.object(module.loop, "paragraph_metadata", return_value={}), \
         patch.object(module.loop, "claim_evidence_contract", return_value={}), \
         patch.object(module.loop, "source_evidence", return_value={"evidence": []}), \
         patch.object(module.loop, "call_json_model", return_value={"reply": "Proposed removal", "candidate_text": candidate, "source_refs": []}):
        result = module.revise(tmp_path, request)
    assert result["candidate_text"] == (candidate if human else "")
    assert result["outcome"] == ("candidate" if human else "validation_failed")
    if human:
        assert result["validation_errors"] == []
        assert result["scientific_changes"] == [{"field": "chemical_identities", "before": ["lcht"], "after": []}]
        assert result["evidence_review"] == "author_review_required"


def test_author_policy_preserves_technical_errors_and_reports_scientific_changes():
    from review_writer_core.paragraph_revision import author_revision_findings
    before = {"chemical_identities": ["cubr"], "numbers": ["10"], "stereo": [], "required_labels": [], "callouts": ["[1]", "[1]"]}
    after = {**before, "chemical_identities": ["cubr2"], "numbers": ["20"], "callouts": ["[1]"]}
    errors, warnings, changes = author_revision_findings(
        ["protected_chemical_identities_changed", "protected_numbers_changed", "protected_callouts_changed", "protected_images_changed", "multiple_prose_blocks"], [], before, after)
    assert errors == ["protected_images_changed", "multiple_prose_blocks"]
    assert len(warnings) == len(changes) == 3


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("candidate,blocked", [
    ("Both results give 94% yield. [4]", False),
    ("Both results give 99% yield. [5]", False),
    ("First result. [4]\n\nSecond result. [4]", True),
    ("Result. [4]\n<!-- paragraph_id: S9-p9 -->", True),
    ("Result. [4] ![New image](unknown.png)", True),
])
def test_chapter_review_warns_but_only_structural_failures_block(tmp_path, monkeypatch, human, candidate, blocked):
    scripts = Path(__file__).resolve().parents[1] / "skills/review-first-draft-feedback-loop/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("chapter_policy_test", scripts / "revise_paragraph.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = "First result gives 94% yield. [4] Second result agrees. [4]"
    first = tmp_path / "04_first_draft"
    first.mkdir()
    (first / "first_draft.md").write_text(original, encoding="utf-8")
    request = {"paragraph_id": "S1-p1", "discussion_text": original, "message": "Revise", "routing": {"mode": "revision"}}
    if human:
        request["section_context"] = {"title": "Results"}
    with patch.object(module.loop, "parse_marked_paragraphs", return_value=[{"paragraph_id": "S1-p1", "text": original}]), \
         patch.object(module.loop, "matrix_rows", return_value={}), \
         patch.object(module.loop, "paragraph_metadata", return_value={}), \
         patch.object(module.loop, "claim_evidence_contract", return_value={}), \
         patch.object(module.loop, "source_evidence", return_value={"evidence": []}), \
         patch.object(module.loop, "call_json_model", return_value={"reply": "Proposal, not source-verified", "candidate_text": candidate, "source_refs": []}):
        result = module.revise(tmp_path, request)
    rejected = blocked or not human
    assert result["outcome"] == ("validation_failed" if rejected else "candidate")
    assert result["candidate_text"] == ("" if rejected else candidate)
    assert result["rejected_candidate_text"] == (candidate if rejected else "")
    if human and not blocked:
        assert "protected_callouts_changed" in result["validation_warnings"]
        assert ("protected_numbers_changed" in result["validation_warnings"]) == ("99%" in candidate)
        assert result["evidence_review"] == "author_review_required"


@pytest.mark.parametrize("mode", ["revision", "question"])
def test_revision_uses_at_most_one_targeted_lookup_and_no_scores(tmp_path, monkeypatch, mode):
    scripts = Path(__file__).resolve().parents[1] / "skills/review-first-draft-feedback-loop/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("dialogue_revision_test", scripts / "revise_paragraph.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "review-projects" / "p"
    first = project / "04_first_draft"
    first.mkdir(parents=True)
    first.joinpath("first_draft.md").write_text("Text", encoding="utf-8")
    source = {"evidence": [{"paper_id": "P1", "title": "Study", "original_passages": [{"ref": "P1:b2", "page": 2, "text": "The reaction works."}]}]}
    with patch.object(module.loop, "parse_marked_paragraphs", return_value=[{"paragraph_id": "S1-p1", "text": "The reaction works."}]), \
         patch.object(module.loop, "matrix_rows", return_value={}), \
         patch.object(module.loop, "paragraph_metadata", return_value={}), \
         patch.object(module.loop, "claim_evidence_contract", return_value={}), \
         patch.object(module.loop, "source_evidence", return_value=source) as retrieve, \
         patch.object(module.loop, "call_json_model", side_effect=[
             {"reply": "Check source", "queries": ["reaction"]},
             {"reply": "Clarified", "candidate_text": "The reaction proceeds.", "source_refs": ["P1:b2"]}]) as model:
        result = module.revise(project, {"paragraph_id": "S1-p1", "discussion_text": "The reaction works.", "message": "Clarify", "routing": {"mode": mode}})
    assert result["outcome"] == ("candidate" if mode == "revision" else "kept_original")
    if mode == "question":
        assert result["candidate_text"] == ""
    assert model.call_count == 2
    assert retrieve.call_count == 2
    assert retrieve.call_args.kwargs["queries"] == ["reaction"]
    assert "score" not in result
    assert result["sources"] == [{"ref": "P1:b2", "paper_id": "P1", "title": "Study", "page": 2,
                                  "text": "The reaction works.", "source_content_hash": ""}]


def test_source_snapshots_only_resolve_known_refs_and_never_guess_pages():
    from review_writer_core.paragraph_revision import revision_sources
    evidence = {"evidence": [{"paper_id": "P2", "source_path": "/private/file", "original_passages": [
        {"ref": "a", "page": 0, "text": "One"}, {"ref": "b", "page": "3", "text": "Two"}]}]}
    sources = revision_sources(["a", "a", "unknown", "b"], evidence)
    assert [s["ref"] for s in sources] == ["a", "b"]
    assert all(s["page"] is None and "source_path" not in s for s in sources)


def test_question_routing_keeps_one_answer_and_explicit_related_reason(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "skills/review-first-draft-feedback-loop/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("scope_test", scripts / "revise_paragraph.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    request = {"route_only": True, "route_allowed_ids": ["S1-p1", "S1-p2"], "message": "Where does this sentence come from?",
        "section_context": {"paragraphs": [{"paragraph_id": "S1-p1"}, {"paragraph_id": "S1-p2"}]}}
    with patch.object(module.loop, "call_json_model", return_value={"mode": "question", "targets": ["S1-p1", "S1-p2"],
         "related": [{"paragraph_id": "S1-p2", "reason": "Explains the same claim"}, {"paragraph_id": "S1-p9", "reason": "Invalid"}]}) as model:
        result = module.revise(tmp_path, request)["routing"]
    assert result["targets"] == ["S1-p1"]
    assert result["related"] == [{"paragraph_id": "S1-p2", "reason": "Explains the same claim"}]
    assert model.call_count == 1
    with patch.object(module.loop, "call_json_model", return_value={"mode": "revision", "targets": ["foreign"]}):
        with pytest.raises(ValueError, match="Invalid chapter evidence anchors"):
            module.revise(tmp_path, request)


def test_chapter_discussion_does_not_inherit_single_paragraph_scope():
    from review_writer_core.paragraph_revision import revision_prompt
    prompt = revision_prompt({"section_context": {"paragraphs": [
        {"paragraph_id": "S01-p1", "text": "First."},
        {"paragraph_id": "S01-p2", "text": "Second."}]},
        "routing": {"mode": "question", "targets": ["S01-p1"]}},
        {"paragraph_id": "S01-p1", "text": "First."}, {"evidence": []})
    assert "entire selected chapter" in prompt
    assert "NOT a restriction" in prompt
    assert "Never ask the user to submit each" in prompt
    assert "revising ONE paragraph" not in prompt
    assert "Other paragraphs are read-only" not in prompt


def test_chapter_revision_keeps_worker_boundary_internal():
    from review_writer_core.paragraph_revision import revision_prompt
    prompt = revision_prompt({"section_context": {"title": "Introduction"},
        "routing": {"mode": "revision"}}, {"text": "First."}, {"evidence": []})
    assert "sibling workers handle the other chapter paragraphs" in prompt
    assert "author-directed chapter editing" in prompt
