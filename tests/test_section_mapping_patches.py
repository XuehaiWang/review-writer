from copy import deepcopy
import json

import pytest

from review_writer_core.stages.sections.source_writing import fingerprint, repair_source_paragraphs


def fixture():
    passage = "Material A achieved 90% conversion at 25 C."
    claim = {"text": passage, "support_spans": [{"evidence_key": "E001", "quote": passage}],
             "result_context": [], "claim_kind": "reported_data"}
    paragraph = {"text": passage, "claims": [claim], "role": "comparison", "reader_takeaway": "Performance"}
    source = {"evidence_key": "a", "paper_id": "A", "content": passage}
    record = {"evidence_key": "E001", "object": "Material A", "conditions": "25 C",
              "result": "90% conversion", "units": ""}
    return paragraph, source, record


@pytest.mark.parametrize("fault", [None, "stale", "duplicate", "wrong_index", "unsupported", "extra_prose"])
def test_record_patch_is_bounded_and_cannot_rewrite_prose(fault):
    paragraph, source, record = fixture()
    invalid_records = [{**record, "result": "99% conversion"}]
    paragraph["claims"][0]["result_context"] = deepcopy(invalid_records)
    original = {"paragraphs": [paragraph]}
    state = {}
    def call(prompt, schema, label):
        payload = json.loads(prompt.split("\n")[-1])
        assert payload["paragraphs"] == []
        assert len(payload["sources"]) == 1
        assert payload["sources"][0]["content"] == source["content"]
        patch = {"paragraph_index": 0, "claim_index": 0,
                 "input_fingerprint": fingerprint(paragraph["claims"][0]), "result_context": [record]}
        if fault == "stale":
            patch["input_fingerprint"] = "old"
        if fault == "wrong_index":
            patch["claim_index"] = 10
        if fault == "unsupported":
            patch["result_context"] = [{**record, "result": "99% conversion"}]
        return {"claim_repairs": [patch, patch] if fault == "duplicate" else [patch],
                "repairs": [{"paragraph_index": 0, "paragraph": {"text": "Overwrite"}}] if fault == "extra_prose" else []}
    result = repair_source_paragraphs(original, task={"paper_roles": [{"paper_id": "A", "presentation": "table"}]},
        shown={"a": source}, aliases={"E001": "a"},
        sources=[{**source, "evidence_key": "E001"}, {"evidence_key": "unrelated", "content": "Other experiment"}],
        domain_terms=[], call=call, state=state, persist=lambda: None)
    assert result["paragraphs"][0]["text"] == paragraph["text"]
    assert result["paragraphs"][0]["claims"][0]["text"] == paragraph["claims"][0]["text"]
    assert original["paragraphs"][0]["claims"][0]["result_context"] == invalid_records
    expected = [record] if fault in {None, "extra_prose"} else invalid_records
    assert result["paragraphs"][0]["claims"][0]["result_context"] == expected
    assert state["mapping_repair_diagnostics"]["record_count"] == 1
    assert state["mapping_repair_diagnostics"]["paragraph_count"] == 0


@pytest.mark.parametrize("paper_roles", [[], [{"paper_id": "A", "presentation": "table"}]])
def test_valid_mapping_needs_no_model_repair(paper_roles):
    paragraph, source, _ = fixture()
    original = {"paragraphs": [paragraph]}
    def call(*args):
        pytest.fail("Correct mapping must not trigger a model request")
    result = repair_source_paragraphs(original, task={"paper_roles": paper_roles}, shown={"a": source}, aliases={"E001": "a"},
        sources=[source], domain_terms=[], call=call, state={}, persist=lambda: None)
    assert result == original


@pytest.mark.parametrize("fault", [None, "empty", "wrong_quote", "duplicate"])
def test_binding_only_patch_keeps_paragraph_and_claim_text(fault):
    paragraph, source, _ = fixture()
    paragraph["claims"][0]["support_spans"][0]["quote"] = "Wrong quotation"
    original = deepcopy(paragraph)
    state = {}
    def call(prompt, schema, label):
        data = json.loads(prompt.split("\n")[-1])
        assert data["paragraphs"] == []
        assert data["claim_problems"][0]["repair_binding"] is True
        spans = [{"evidence_key": "E001", "quote": source["content"]}]
        if fault == "empty":
            spans = []
        if fault == "wrong_quote":
            spans[0]["quote"] = "Another invented quotation"
        patch = {"paragraph_index": 0, "claim_index": 0,
                 "input_fingerprint": fingerprint(original["claims"][0]),
                 "support_spans": spans, "result_context": None}
        return {"claim_repairs": [patch, patch] if fault == "duplicate" else [patch]}
    result = repair_source_paragraphs({"paragraphs": [paragraph]}, task={},
        shown={"a": source}, aliases={"E001": "a"}, sources=[source],
        domain_terms=[], call=call, state=state, persist=lambda: None)
    assert result["paragraphs"][0]["text"] == original["text"]
    claim = result["paragraphs"][0]["claims"][0]
    assert claim["text"] == original["claims"][0]["text"]
    assert claim["support_spans"][0]["quote"] == (source["content"] if fault is None else "Wrong quotation")
    assert state["mapping_repair_diagnostics"]["accepted_claim_count"] == (1 if fault is None else 0)


def test_checkpoint_reports_drafts_without_marking_them_complete(tmp_path):
    from types import SimpleNamespace
    from review_writer_api.job_handlers.sections import SectionJobHandlers
    status, checkpoint = tmp_path / "status.json", tmp_path / "checkpoint.json"
    status.write_text(json.dumps({"current": 0, "total": 7}))
    checkpoint.write_text(json.dumps({"authoring_states": {
        "S01": {"state": {"proposed": {"paragraphs": [{"text": "Draft"}]}}},
        "S02": {"state": {"proposed": {"paragraphs": []}}},
    }}))
    updates, progress = [], []
    context = SimpleNamespace(report_partial_result=updates.append,
                              report_progress=lambda *args: progress.append(args))
    callback = SectionJobHandlers._section_progress_callback(context, status, checkpoint)
    callback()
    callback()
    assert progress == [(0, 7)]
    assert updates[0]["section_progress"]["drafted_section_ids"] == ["S01"]
