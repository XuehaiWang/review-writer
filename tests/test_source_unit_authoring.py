"""Offline contracts: selection is exact, not a scientific endorsement."""
from copy import deepcopy
import json

import pytest

from review_writer_core.stages.sections import source_writing as writer
from review_writer_core.stages.sections.source_units import (
    UNIT_FORMAT, QUOTE_FORMAT, build_source_units, resolve_unit_response, unit_schema, evidence_uses,
)
from review_writer_core.stages.sections.coverage import checkpoint_authoring_settings, check_historical_resume


def source(text="The intervention was feasible in this pilot cohort."):
    return {"evidence_key": "owned", "paper_id": "P1", "chunk_id": "chunk",
            "content": text, "source_lineage_hash": "revision1", "claim_eligible": True}


def args(**overrides):
    return {"section_id": "S1", "task": {"section_id": "S1", "primary_papers": ["P1"]},
            "evidence": [source()], "context": "Confirmed scope", "audit_mode": "selective",
            "citation_format": UNIT_FORMAT, **overrides}


def proposal(prompt, *, uses=True):
    payload = json.loads(prompt[prompt.index('{"task":'):])
    uid = payload["sources"][0]["source_units"][0]["unit_id"]
    text = "Feasibility was demonstrated within the pilot cohort."
    paragraph = {"reader_takeaway": "Feasibility is bounded to the tested cohort.", "role": "anchor_case",
                 "text": text, "claims": [{"text": text, "claim_kind": "reported_finding",
                 "support_spans": [{"unit_id": uid}], "fact_ids": [], "review_reasons": [], "result_context": []}]}
    if uses:
        paragraph["evidence_uses"] = [{"source_refs": [uid], "purpose": "Establish feasibility, not general effectiveness."}]
    return {"paragraphs": [paragraph]}


@pytest.mark.parametrize("text,count", [
    ("A value of 1.5 mmol with $Cu_2$ was tested. Not a mechanism proof.", 1),
    ("Reported observation.\n\nHowever, the proposed mechanism is unconfirmed.", 2),
    ("| Material | Yield |\n| A | 50% |\n\nNot comparable to B.", 1),
    ("中文结果。\n跨行内容，非因果证明。", 1),
])
def test_units_preserve_exact_text_and_qualifications(text, count):
    rows = [source(text)]
    display, mapping = build_source_units(rows)
    assert "content" not in display[0]
    assert len(mapping) == count
    for unit in mapping.values():
        assert unit["quote"] == text[unit["start"]:unit["end"]]
    assert rows == [source(text)]


def test_mapping_identity_covers_source_ownership_version_order_and_text():
    initial = [source(), {**source("A separate result."), "paper_id": "P2"}]
    ids = set(build_source_units(initial)[1])
    for rows in (list(reversed(initial)), [{**initial[0], "source_lineage_hash": "revision2"}, initial[1]],
                 [{**initial[0], "paper_id": "foreign"}, initial[1]],
                 [{**initial[0], "content": "Changed text"}, initial[1]]):
        assert ids.isdisjoint(build_source_units(rows)[1])


@pytest.mark.parametrize("bad", [{"unit_id": "missing"}, {"unit_id": []},
    {"evidence_key": "owned", "quote": "The intervention was feasible in this pilot cohort."}, None])
def test_invalid_or_legacy_spans_are_not_accepted_in_unit_contract(bad):
    _, units = build_source_units([source()])
    result = resolve_unit_response({"claims": [{"support_spans": [bad]}]}, units)
    assert not writer.resolve_spans(result["claims"][0]["support_spans"], {"owned": source()})


def test_mixed_format_not_silently_preferred_and_repair_schema_is_versioned():
    _, units = build_source_units([source()])
    uid = next(iter(units))
    result = resolve_unit_response({"support_spans": [{"unit_id": uid, "quote": units[uid]["quote"]}]}, units)
    assert not writer.resolve_spans(result["support_spans"], {"owned": source()})
    schema = unit_schema(writer.REPAIR_SCHEMA)
    span = schema["properties"]["claim_repairs"]["items"]["properties"]["support_spans"]
    assert span["items"]["required"] == ["unit_id"]
    assert "null" in span["type"]
    assert writer.SPAN["required"] == ["evidence_key", "quote"]


@pytest.mark.parametrize("uses", [True, False, "bad"])
def test_same_call_record_is_optional_and_prose_remains_source_checked(uses):
    calls, saved = [], {}
    def model(prompt, schema, label):
        calls.append(label)
        assert label == "section-source-writing"
        assert "Copy quotes literally" not in prompt
        assert "source_units" in prompt
        para = schema["properties"]["paragraphs"]["items"]
        assert "evidence_uses" not in para["required"]
        assert para["properties"]["claims"]["items"]["properties"]["support_spans"]["items"]["required"] == ["unit_id"]
        value = proposal(prompt, uses=uses)
        if uses == "bad":
            value["paragraphs"][0]["evidence_uses"] = [{"source_refs": ["unknown"], "purpose": "bad"}]
        return value
    def save(state):
        saved.clear()
        saved.update(deepcopy(state))
    first = writer.write_from_sources(**args(call=model, save_state=save))
    second = writer.write_from_sources(**args(call=model, save_state=save, resume_state=deepcopy(saved)))
    assert first == second
    assert calls == ["section-source-writing"]
    assert writer.valid_source_claim(first[0]["claims"][0], {"owned": source()})
    if uses is True:
        assert first[0]["paragraphs"][0]["evidence_uses"][0]["source_refs"] == ["owned"]
    if uses == "bad":
        assert first[0]["paragraphs"][0]["evidence_use_issues"]


def test_retired_selection_checkpoint_cannot_silently_replay_as_standard_writing():
    with pytest.raises(RuntimeError, match="Retired experimental writing checkpoint"):
        writer.write_from_sources(**args(call=lambda *a: pytest.fail("must not call provider"),
                                         resume_state={"focused_input_version": "experimental-findings/1"}))


def test_saved_prompt_and_mapping_reused_after_wording_change(monkeypatch):
    saved, calls = {}, []
    def save(state):
        saved.clear()
        saved.update(deepcopy(state))
    def deferred(prompt, schema, label):
        calls.append((prompt, schema))
        raise RuntimeError("simulated interruption before response")
    with pytest.raises(RuntimeError, match="interruption"):
        writer.write_from_sources(**args(call=deferred, save_state=save))
    assert saved["request_snapshot"]["units"]
    monkeypatch.setattr(writer, "ARGUMENT_USE_GUIDANCE", "A newer policy\n")
    def resumed(prompt, schema, label):
        assert (prompt, schema) == calls[0]
        return proposal(prompt)
    writer.write_from_sources(**args(call=resumed, resume_state=saved))


def test_changed_scientific_input_never_silently_replays_old_paid_request():
    saved = {}
    writer.write_from_sources(**args(call=lambda p, s, l: proposal(p), save_state=lambda value: saved.update(value)))
    original = deepcopy(saved)
    with pytest.raises(RuntimeError, match="inputs changed"):
        writer.write_from_sources(**args(context="Different scientific scope", resume_state=saved,
            call=lambda *a: pytest.fail("must not call provider")))
    assert saved == original


def test_valid_source_id_does_not_override_negative_scientific_verdict():
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return proposal(prompt)
        return {"claims": [{"claim_id": "S1-p1-C01", "status": "unsupported", "text": "",
            "reason": "Simulated scientific conflict", "conflict_quote": source()["content"]}]}
    writing, _, report = writer.write_from_sources(**args(call=model, audit_mode="full"))
    assert not writing["claims"]
    assert report["omitted"]
    assert "section-used-claim-check" in calls


def test_invalid_unit_uses_existing_bounded_repair_with_same_wire_contract():
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            value = proposal(prompt)
            value["paragraphs"][0]["claims"][0]["support_spans"] = [{"unit_id": "unknown"}]
            return value
        if label == "section-used-claim-check":
            return {"claims": [{"claim_id": "S1-p1-C01", "status": "supported", "text": "", "reason": "Supported."}]}
        assert label == "section-source-mapping-repair"
        assert "Return support_spans as unit_id" in prompt
        payload = json.loads(prompt[prompt.index('{"paragraphs":'):])
        problem = payload["claim_problems"][0]
        uid = payload["sources"][0]["source_units"][0]["unit_id"]
        assert schema["properties"]["claim_repairs"]["items"]["properties"]["support_spans"]["items"]["required"] == ["unit_id"]
        return {"repairs": [], "claim_repairs": [{"paragraph_index": problem["paragraph_index"],
            "claim_index": problem["claim_index"], "input_fingerprint": problem["input_fingerprint"],
            "support_spans": [{"unit_id": uid}], "result_context": None}]}
    writing, _, report = writer.write_from_sources(**args(call=model))
    assert writer.valid_source_claim(writing["claims"][0], {"owned": source()})
    assert report["mapping_repair_attempted"]
    assert calls == ["section-source-writing", "section-source-mapping-repair", "section-used-claim-check"]


def test_unselected_neighbor_is_not_used_to_fill_missing_anchor():
    text = "The intervention was feasible in this pilot cohort.\n\nA different population had 90% recovery."
    captured = []
    def model(prompt, schema, label):
        if label == "section-source-writing":
            response = proposal(prompt)
            response["paragraphs"][0]["claims"][0]["text"] = "Recovery reached 90% in the pilot cohort."
            response["paragraphs"][0]["text"] = response["paragraphs"][0]["claims"][0]["text"]
            return response
        payload = json.loads(prompt[prompt.index('{"claims":'):])
        captured.extend(payload["claims"])
        return {"claims": [{"claim_id": "S1-p1-C01", "status": "unresolved", "text": "", "reason": "Wrong population."}]}
    _, _, report = writer.write_from_sources(**args(call=model, evidence=[source(text)]))
    assert captured
    assert all("90%" not in ref["quote"] for claim in captured for ref in claim["evidence_refs"])
    assert not report["binding_repairs"]


def test_legacy_no_snapshot_requires_exact_old_fingerprint():
    saved = {}
    def model(prompt, schema, label):
        return {"paragraphs": [{"role": "anchor_case", "text": source()["content"], "claims": [{
            "text": source()["content"], "claim_kind": "reported_finding", "support_spans": [{
                "evidence_key": "E001", "quote": source()["content"]}]}]}]}
    options = args(call=model, citation_format=QUOTE_FORMAT, save_state=lambda s: saved.update(s))
    writer.write_from_sources(**options)
    current_prompt = saved.pop("request_snapshot")["prompt"]
    prefix, body = current_prompt.split('{"task":', 1)
    payload = json.loads('{"task":' + body)
    payload.pop('question_evidence_navigation', None)  # Absent in the historical wire format.
    payload['task'] = writer.build_section_writing_task(options['task'])
    old_prompt = prefix.replace(writer.ARGUMENT_USE_GUIDANCE, '', 1) + json.dumps(payload, ensure_ascii=False)
    saved["input_fingerprint"] = writer.fingerprint({"prompt": old_prompt, "evidence": options["evidence"],
        "mode": options["audit_mode"], "version": writer.AUTHORING_VERSION, "domain_terms": []})
    writer.write_from_sources(**{**options, "call": lambda *a: pytest.fail("already completed"), "resume_state": deepcopy(saved)})
    saved.pop("request_snapshot", None)
    saved["input_fingerprint"] = "cannot-reconstruct"
    with pytest.raises(RuntimeError, match="Historical section"):
        writer.write_from_sources(**{**options, "call": lambda *a: pytest.fail("unsafe replay"), "resume_state": saved})


def test_checkpoint_settings_pin_rules_without_changing_scientific_input_matching():
    new = checkpoint_authoring_settings({}, project_id="p", rules="old", synthesis_rules="old")
    assert new["citation_format"] == UNIT_FORMAT
    old = checkpoint_authoring_settings({"project_id": "p", "entries": {"S1": {}}},
        project_id="p", rules="old", synthesis_rules="old")
    assert old["citation_format"] == QUOTE_FORMAT
    assert checkpoint_authoring_settings({"project_id": "p", "authoring_settings": new},
        project_id="p", rules="new", synthesis_rules="new") == new
    assert evidence_uses(None, {}) == ([], [])


def test_historical_mismatch_preserves_paid_state_instead_of_silently_discarding_it():
    checkpoint = {"project_id": "p", "authoring_states": {"S1": {"section_input": "old", "state": {"proposed": "paid"}}}}
    before = deepcopy(checkpoint)
    with pytest.raises(ValueError, match="preserved"):
        check_historical_resume(checkpoint, {"S1": "new"}, project_id="p", rejected_entries={})
    assert checkpoint == before
    check_historical_resume(checkpoint, {"S1": "old"}, project_id="p", rejected_entries={})


def test_current_scope_separates_planning_status_without_losing_user_science():
    section = {"heading": "Treatment outcomes", "writing_objective": "Abstract only; retrieve results. Exclude pediatric trials.",
               "review_problem": "What limits benefit?", "avoid_points": ["Do not pool different populations."],
               "primary_papers": ["P1", "P2", "P3"]}
    before = deepcopy(section)
    evidence = [{"paper_id": "P1", "source_channel": "body", "assertion_ceiling": "direct_source_report"},
                {"paper_id": "P2", "source_channel": "abstract", "assertion_ceiling": "abstract_report_only"},
                {"paper_id": "P3"}]
    projected = writer.build_section_writing_task(section, current_evidence=evidence)
    assert "writing_objective" not in projected
    assert projected["planning_snapshot"]["original_objective"] == section["writing_objective"]
    assert projected["argument_question"] == section["review_problem"]
    assert projected["avoid_points"] == section["avoid_points"]
    papers = projected["current_evidence_scope"]["papers"]
    assert papers[1]["assertion_ceilings"] == ["abstract_report_only"]
    assert papers[2]["source_channels"] == []
    assert section == before


def test_chapter_navigation_does_not_repeat_current_or_other_paragraph_work_orders():
    rows = [{"section_id": "S1", "writing_objective": "Current task"},
            {"section_id": "S2", "heading": "Other system", "writing_objective": "Exclude design X",
             "avoid_points": ["Preserve exception Y"], "paragraph_tasks": ["Unrelated checklist"]}]
    before = deepcopy(rows)
    result = writer.build_chapter_navigation(rows, section_id="S1")
    assert len(result) == 1 and result[0]["section_id"] == "S2"
    assert "paragraph_tasks" not in result[0]
    assert result[0]["planning_snapshot"]["original_objective"] == "Exclude design X"
    assert result[0]["avoid_points"] == ["Preserve exception Y"]
    assert rows == before


def test_new_writer_requests_qualified_findings_without_new_fields_or_calls():
    class Captured(Exception): pass
    def capture(prompt, schema, label):
        assert label == "section-source-writing"
        assert "retain material adverse outcomes and qualifications" in prompt
        assert "combine their evidence in one" in prompt
        assert "Select mechanistic steps only when they explain" in prompt
        assert writer.EVIDENCE_USE_GUIDANCE not in prompt
        payload = json.loads(prompt[prompt.index('{"task":'):])
        assert "planning_snapshot" in payload["task"]
        assert "current_evidence_scope" in payload["task"]
        assert "evidence_uses" not in schema["properties"]["paragraphs"]["items"]["required"]
        raise Captured
    with pytest.raises(Captured):
        writer.write_from_sources(**args(call=capture))
