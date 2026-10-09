from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from review_writer_core.stages.sections.source_writing import (
    CONTRACT, complete_cited_anchor_spans, write_from_sources, valid_source_claim,
)
from review_writer_core.model_gateway_client import DeferredModelCall
from review_writer_core.stages.sections.coverage import reusable_section_entries
from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.errors import WorkflowValidationError


SCRIPT = Path(__file__).resolve().parents[1] / "skills/review-section-drafting-figure-picking/scripts/generate_section_drafts.py"
spec = importlib.util.spec_from_file_location("source_passage_pipeline", SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def source(key="a", paper="A", content="Catalyst A degraded 90% of the pollutant at 25 C after 60 minutes."):
    return {"evidence_key": key, "evidence_id": key, "paper_id": paper, "chunk_id": key,
            "content": content, "source_lineage_hash": "v1", "page_start": 2, "page_end": 2,
            "claim_eligible": True, "assertion_ceiling": "direct_source_report", "support_level": "direct"}


def task():
    return {"section_id": "S01", "heading": "Catalysts", "section_role": "body", "primary_papers": ["A"],
            "allowed_papers": ["A", "B"], "questions_to_answer": ["What was reported?"], "writing_objective": "Compare reported conditions."}


def first_unit_span(prompt):
    payload = json.loads(prompt[prompt.index('{"task":'):])
    return {"unit_id": payload["sources"][0]["source_units"][0]["unit_id"]}


def test_partial_upgrade_keeps_completed_chapter_and_resumes_original_request(tmp_path, monkeypatch):
    stage = tmp_path / "review-projects/demo/02_section_drafting"
    planning = stage.parent / "01_matrix_outline"
    stage.mkdir(parents=True)
    planning.mkdir()
    tasks = [{**task(), "section_id": sid, "heading": sid} for sid in ("S01", "S02")]
    for path, payload in {
        stage / "section_tasks.json": tasks,
        stage / "section_evidence.json": {"sections": [
            {"section_id": t["section_id"], "retrieval_mode": "lexical", "hits": [source()]} for t in tasks]},
        planning / "literature_matrix.json": {"rows": [{"paper_id": "A", "title": "Pilot"}]},
        planning / "section_blueprint.json": {"sections": tasks},
    }.items():
        path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pipeline.sys, "argv", [str(SCRIPT), "--review-root", str(tmp_path),
        "--project-id", "demo", "--api-key", "test", "--model", "test", "--audit-mode", "full"])
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *a: {})
    monkeypatch.setattr(pipeline, "load_blueprint_rule_pack", lambda *a: "Original rules")
    monkeypatch.setattr(pipeline, "load_cross_study_synthesis_skill", lambda *a: "Original synthesis")
    failed, writes = [], []
    def model(prompt, schema, *a, label, **kw):
        if label == "section-source-writing":
            payload = json.loads(prompt[prompt.index('{"task":'):])
            writes.append(payload["task"]["heading"])
            return {"paragraphs": [{"reader_takeaway": "Pilot result", "role": "anchor_case",
                "text": source()["content"], "claims": [{"text": source()["content"],
                "claim_kind": "reported_finding", "support_spans": [first_unit_span(prompt)]}]}]}
        payload = json.loads(prompt[prompt.index('{"claims":'):])
        cid = payload["claims"][0]["claim_id"]
        if cid.startswith("S02") and not failed:
            failed.append(True)
            raise RuntimeError("Temporary provider error")
        return {"claims": [{"claim_id": cid, "status": "supported", "text": "", "reason": ""}]}
    monkeypatch.setattr(pipeline, "call_structured_llm", model)
    with pytest.raises(SystemExit, match="Retry the job"):
        pipeline.main()
    path = stage / "section_checkpoints.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    assert set(before["entries"]) == {"S01"}
    output = before['entries']['S01']['output']
    from review_writer_core.manuscript_coherence import current_records
    assert current_records(output['paragraphs'], output['content_records'])['S01-p1']['reader_takeaway'] == 'Pilot result'
    assert before["authoring_states"]["S02"]["state"]["request_snapshot"]
    monkeypatch.setattr(pipeline, "load_blueprint_rule_pack", lambda *a: "Upgraded rules")
    monkeypatch.setattr(pipeline, "load_cross_study_synthesis_skill", lambda *a: "Upgraded synthesis")
    assert pipeline.main() == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["entries"]["S01"] == before["entries"]["S01"]
    assert set(after["entries"]) == {"S01", "S02"}
    assert sorted(writes) == ["S01", "S02"]


@pytest.mark.parametrize("papers", [["A"], ["A", "B"]])
def test_editorial_selection_preserves_sources_exceptions_and_legacy_plans(papers):
    rows = [source(paper=p, key=p, content=("Pd dba ·CHCl gave a result." if p == "A"
                                          else "The same intervention failed in a second population.")) for p in papers]
    current = {**task(), "primary_papers": papers,
               "avoid_points": ["Report the exact conditions when supported."],
               "requested_length": "Short"}
    before = deepcopy((current, rows))
    class Captured(Exception): pass
    calls = []
    def capture(prompt, schema, label):
        calls.append(label)
        payload = json.loads(prompt[prompt.index('{"task":'):])
        assert len(payload["sources"]) == len(rows)
        assert [s["content"] for s in payload["sources"]] == [s["content"] for s in rows]
        assert payload["task"]["avoid_points"] == current["avoid_points"]
        assert "Never guess missing subscripts" in prompt
        assert "Revisit it only to advance a distinct supported implication" in prompt
        assert "without repeating conditions and results" in prompt
        assert "when absent, reason directly from the supplied evidence" in prompt
        assert "retaining decisive exceptions" in prompt
        assert "explicit user-requested detail" in prompt
        assert "smallest sufficient" in prompt
        assert "A paragraph that reports no method of its own may use an empty result_context" in prompt
        assert "the reader table is projected from result_context alone" in prompt
        assert list(schema["properties"]["paragraphs"]["items"]["properties"]) == [
            "reader_takeaway", "role", "evidence_uses", "text", "claims"]
        assert prompt.count("Revisit it only") == 1
        raise Captured
    with pytest.raises(Captured):
        write_from_sources(section_id="S01", task=current, evidence=rows + [rows[0]], context="", call=capture)
    assert calls == ["section-source-writing"]
    assert (current, rows) == before


def test_editorial_policy_does_not_turn_empty_evidence_into_a_model_call():
    def no_call(*args, **kwargs):
        pytest.fail("Empty evidence must not incur a model call")
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=[], context="", call=no_call)
    assert writing["claims"] == []
    assert report["omitted"] == [{"reason": "no_registered_passage"}]


def test_retrieval_checks_remain_on_original_task_but_not_chapter_responsibilities():
    from review_writer_core.section_narrative_contracts import build_section_writing_task
    from review_writer_core.stages.sections.execution import chapter_responsibilities
    original = {**task(), "review_problem": "What limits transfer to another population?",
                "questions_to_answer": ["Find all reported doses and response rates."],
                "retrieval_directions": ["Retrieve the full results and negative cases."]}
    snapshot = deepcopy(original)
    projected = build_section_writing_task(original)
    assert projected["argument_question"] == original["review_problem"]
    assert projected["evidence_checks"]["questions"] == original["questions_to_answer"]
    assert "not_a_prose_checklist" in projected["evidence_checks"]["usage"]
    responsibilities = chapter_responsibilities([original])[0]
    assert "questions_to_answer" not in responsibilities
    assert responsibilities["review_problem"] == original["review_problem"]
    assert original == snapshot


def test_shared_synthesis_resource_no_longer_prescribes_repeated_five_step_units():
    rules = pipeline.load_cross_study_synthesis_skill()
    assert "not a five-step sequence" in rules
    assert "prefer this order" not in rules
    assert "Do not directly compare" in rules


def test_sparse_qualitative_prose_is_published_and_reused_without_extra_review():
    row = source(content="The intervention was feasible in this pilot cohort.")
    saved, calls = {}, []
    def model(prompt, schema, label):
        calls.append(label)
        assert label == "section-source-writing"
        return {"paragraphs": [{"reader_takeaway": "Pilot feasibility is bounded to this cohort.",
                                "role": "anchor_case", "text": row["content"],
                                "claims": [prose_claim(row["content"], row["content"])]}]}
    def save(state):
        saved.clear()
        saved.update(deepcopy(state))
    args = dict(section_id="S01", task=task(), evidence=[row], context="", call=model,
                audit_mode="selective", save_state=save)
    first = write_from_sources(**args)
    second = write_from_sources(**args, resume_state=deepcopy(saved))
    assert calls == ["section-source-writing"]
    assert first == second
    assert first[0]["claims"][0]["claim"] == row["content"]
    assert first[2]["omitted"] == []


@pytest.mark.parametrize("channel,ceiling", [("abstract", "abstract_report_only"), ("body", "direct_source_report")])
def test_writer_projects_current_sources_without_retrieval_orders_or_repeated_objective(channel, ceiling):
    from review_writer_core.section_narrative_contracts import build_section_writing_guidance
    current_task = {**task(), "core_argument": task()["writing_objective"],
                    "retrieval_directions": ["Retrieve the missing full text"],
                    "avoid_points": ["Keep the explicit abstract-only review scope."] * 2,
                    "notes": "User note: do not compare incomparable interventions.",
                    "requested_length": "Concise, but preserve limitations",
                    "paper_roles": [{"paper_id": "A", "role": "main_progress", "reason": "Full text remains to be retrieved"}]}
    row = {**source(), "source_channel": channel, "assertion_ceiling": ceiling}
    before = deepcopy((current_task, row))
    class Captured(Exception): pass
    calls = []
    def capture(prompt, schema, label):
        calls.append(label)
        payload = json.loads(prompt[prompt.index('{"task":'):])
        assert "retrieval_directions" not in payload["task"]
        assert "core_argument" not in payload["task"]
        assert payload["task"]["planning_snapshot"]["original_objective"] == task()["writing_objective"]
        assert len(payload["task"]["avoid_points"]) == 1
        assert payload["sources"][0]["assertion_ceiling"] == ceiling
        assert payload["sources"][0]["source_channel"] == channel
        assert payload["writing_guidance"] == build_section_writing_guidance(current_task)
        assert "planning_snapshot_not_current_retrieval_status" in payload["writing_guidance"]["usage"]
        assert payload["writing_guidance"]["notes"] == current_task["notes"]
        assert payload["writing_guidance"]["requested_length"] == current_task["requested_length"]
        assert "Available passages do not imply complete full-text coverage" in prompt
        assert "retain explicit user scope" in prompt
        raise Captured
    with pytest.raises(Captured):
        write_from_sources(section_id="S01", task=current_task, evidence=[row], context="", call=capture)
    assert calls == ["section-source-writing"]
    assert (current_task, row) == before


def test_projection_keeps_distinct_arguments_and_non_english_user_constraints():
    from review_writer_core.section_narrative_contracts import build_section_writing_task
    current_task = {**task(), "core_argument": "Do not generalize this result.",
                    "avoid_points": ["仅限摘要证据", "不要删除失败案例"],
                    "questions_to_answer": ["为何失败？", "为何失败？", "边界是什么？"]}
    result = build_section_writing_task(current_task)
    assert result["core_argument"] == current_task["core_argument"]
    assert result["avoid_points"] == current_task["avoid_points"]
    assert result["evidence_checks"]["questions"] == ["为何失败？", "边界是什么？"]
    assert "questions_to_answer" not in result


def test_hosted_writer_uses_shared_schema_envelope(monkeypatch):
    from review_writer_core.model_gateway_client import structured_json_prompt
    captured = []
    schema = {"type": "object", "description": "中文字段"}
    original = deepcopy(schema)
    monkeypatch.setattr(pipeline, "gateway_configured", lambda: True)
    def gateway(prompt, **kwargs):
        captured.append(prompt)
        return {"paragraphs": []}
    monkeypatch.setattr(pipeline, "call_gateway_json", gateway)
    result = pipeline.call_structured_llm("Write", schema, "unused", "unused", "unused",
                                          label="test", schema_name="test")
    assert result == {"paragraphs": []}
    assert captured == [structured_json_prompt("Write", schema)]
    assert "中文字段" in captured[0]
    assert schema == original


def test_empty_source_writer_gets_one_bounded_repair():
    calls = []
    claim = "At 25 C, Catalyst A achieved 90% degradation after 60 minutes."
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            if calls.count(label) == 1:
                return {"paragraphs": []}
            assert "EMPTY-DRAFT RECOVERY" in prompt
            return {"paragraphs": [{"claims": [{"text": claim, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": source()["content"]}]}]}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=model)
    assert writing["paragraphs"] and writing["claims"][0]["claim"] == claim
    assert calls.count("section-source-writing") == 2
    assert not report["unresolved"]


def test_repeated_empty_source_writer_is_not_completed_evidence_notice():
    calls, saved = [], {}
    def model(prompt, schema, label):
        calls.append(label)
        return {"paragraphs": []}
    def save(state):
        saved.clear()
        saved.update(deepcopy(state))
    with pytest.raises(RuntimeError, match="no paragraphs despite registered passages"):
        write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=model, save_state=save)
    assert len(calls) == 2
    assert "proposed" not in saved
    assert saved["empty_repair_attempted"]


def test_no_registered_sources_does_not_call_empty_repair():
    def model(*args):
        pytest.fail("No source evidence must not trigger invented prose")
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=[], context="", call=model)
    assert writing["paragraphs"] == []
    assert report["omitted"][0]["reason"] == "no_registered_passage"


def test_audited_rewrite_preserves_source_binding_without_an_extra_call():
    replacement = "After 60 minutes at 25 C, Catalyst A achieved 90% pollutant degradation."
    writing, generated, report, calls = write(audit_status="rewritten", replacement=replacement)
    assert writing["claims"][0]["claim"] == replacement
    assert writing["claims"][0]["evidence_refs"][0]["quote"] == source()["content"]
    assert valid_source_claim(writing["claims"][0], {"a": source()})
    assert len(calls) == 2
    assert report["omitted"] == []


@pytest.mark.parametrize("formula,plain", [
    (r"$\mathrm { P d } ( \mathrm { P P h } _ { 3 } ) _ { 4 }$", "Pd(PPh3)4"),
    (r"$\mathrm { C O } _ { 2 }$", "CO2"),
    (r"$\mathrm { C o C l } _ { 2 }$", "CoCl2"),
])
def test_same_cited_passage_recovers_tex_formula_outside_short_quote(formula, plain):
    passage = f"The reaction used {formula} to form the reported allene."
    claim = f"The reaction used {plain} to form the reported allene."
    evidence = [source(content=passage)]
    calls = []
    def model(_prompt, _schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": claim, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": "The reaction used"}]}]}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}
    writing, _, report = write_from_sources(
        section_id="S01", task=task(), evidence=evidence, context="", call=model
    )
    assert writing["claims"][0]["claim"] == claim
    assert writing["claims"][0]["evidence_refs"][0]["quote"] == passage
    assert valid_source_claim(writing["claims"][0], {"a": evidence[0]})
    assert report["unresolved"] == []
    assert calls == ["section-source-writing", "section-used-claim-check"]


def test_missing_anchor_in_cited_passage_repairs_a_supported_verdict():
    passage = "A palladium catalyst afforded the allene in 92% yield."
    claim = "Pd(PPh3)4 afforded the allene in 92% yield."
    calls = []
    def model(prompt, _schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": claim, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": passage}]}]}]}
        if label == "section-used-claim-check":
            return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}
        assert label == "section-source-content-repair"
        assert "named chemical entity" in prompt
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "rewritten",
                            "text": passage, "reason": "Use only the reported catalyst identity."}]}
    writing, _, report = write_from_sources(
        section_id="S01", task=task(), evidence=[source(content=passage)],
        context="", call=model,
    )
    assert writing["claims"][0]["claim"] == passage
    assert report["unresolved"] == []
    assert calls == ["section-source-writing", "section-used-claim-check", "section-source-content-repair"]


def test_unresolved_source_recheck_does_not_repeat_paid_repair_for_changed_verdict_wording():
    passage = "A palladium catalyst afforded the allene in 92% yield."
    claim = "Pd(PPh3)4 afforded the allene in 92% yield."
    saved, calls = {}, []

    def save(value):
        saved.clear()
        saved.update(deepcopy(value))

    def model(_prompt, _schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": claim, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": passage}]}]}]}
        if label == "section-used-claim-check":
            return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "",
                                "reason": f"Source check {calls.count(label)}."}]}
        assert label == "section-source-content-repair"
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "unresolved", "text": "",
                            "reason": "Catalyst identity cannot be verified."}]}

    args = dict(section_id="S01", task=task(), evidence=[source(content=passage)],
                context="", call=model, save_state=save)
    _, _, first = write_from_sources(**args)
    assert first["unresolved"]
    _, _, second = write_from_sources(**args, resume_state=deepcopy(saved))
    assert second["unresolved"]
    assert calls.count("section-source-writing") == 1
    assert calls.count("section-used-claim-check") == 2
    assert calls.count("section-source-content-repair") == 1


def test_uncited_passage_cannot_supply_missing_anchor():
    cited = source(content="A palladium catalyst afforded an allene.")
    uncited = source("b", "B", "Pd(PPh3)4 was used in a separate study.")
    refs = [{"evidence_key": "a", "quote": cited["content"]}]
    checked, repairs = complete_cited_anchor_spans(
        "Pd(PPh3)4 afforded an allene.", refs, {"a": cited, "b": uncited}
    )
    assert checked == refs
    assert repairs == []


def test_selective_anchor_check_repairs_legacy_incomplete_audit_and_resumes_same_request():
    evidence = [source(content="An allene was obtained using a zinc catalyst.")]
    good, bad = "An allene was obtained.", "ZnI2 afforded the allene in 98% yield."
    saved, audit_prompts = {}, []
    def save(value):
        saved.clear()
        saved.update(deepcopy(value))
    def model(prompt, schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": text, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "a", "quote": evidence[0]["content"]}]}
                for text in (good, bad)]}]}
        return {"claims": [
            {"claim_id": "S01-p1-C01", "status": "supported", "text": ""},
            {"claim_id": "S01-p1-C02", "status": "narrowed",
             "text": "A zinc catalyst was used to obtain the allene.", "reason": "Only catalyst class is supported."}]}
    args = dict(section_id="S01", task=task(), evidence=evidence, context="",
                audit_mode="selective", save_state=save)
    write_from_sources(**args, call=model)
    # Model the old checkpoint: a cached audit omitted the anchor-mismatched
    # sentence, but a different sentence was verified successfully.
    saved["accepted_claims"] = [c for c in saved["accepted_claims"] if c["claim_id"] == "S01-p1-C01"]
    saved["audit"] = {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": ""}]}
    saved["scientific_ids"] = []
    saved["target_ids"] = ["S01-p1-C01"]
    saved["repair_attempted"] = True
    def deferred(prompt, schema, label):
        assert label == "section-used-claim-check"
        audit_prompts.append(prompt)
        raise DeferredModelCall("anchor-audit")
    with pytest.raises(DeferredModelCall):
        write_from_sources(**args, call=deferred, resume_state=deepcopy(saved))
    def resumed(prompt, schema, label):
        assert label == "section-used-claim-check"
        audit_prompts.append(prompt)
        payload = json.loads(prompt[prompt.index('{"claims":'):])
        assert [c["claim_id"] for c in payload["claims"]] == ["S01-p1-C02"]
        assert payload["review_reasons"]["S01-p1-C02"] == ["source_anchor_mismatch"]
        return model(prompt, schema, label)
    writing, _, report = write_from_sources(**args, call=resumed, resume_state=deepcopy(saved))
    assert audit_prompts[0] == audit_prompts[1]
    assert len(writing["claims"]) == 2
    assert not report["unresolved"]
    assert valid_source_claim(writing["claims"][1], {"a": evidence[0]})


@pytest.mark.parametrize("replacement", [
    "The reaction afforded an allene in 82% yield using ZnI2.",
    "An allene was obtained using ZnI2 in 82% yield.",
])
def test_source_value_correction_is_verified_and_publishable(replacement):
    evidence = [source(content="The reaction afforded an allene in 82% yield using ZnI2.")]
    writing, generated, report, _ = write(
        evidence, text="The reaction afforded an allene in 98% yield using ZnBr2.",
        audit_status="rewritten", replacement=replacement)
    assert writing["claims"][0]["claim"] == replacement
    assert report["narrowed"] and not report["unresolved"]
    assert valid_source_claim(writing["claims"][0], {"a": evidence[0]})
    package, built, synthesis, plan = bundle(writing, generated, evidence)
    SectionsService._validate_academic_bundle(
        {"tasks": [task()], "blueprint": {"schema_version": 2}}, built, synthesis, plan, package)


@pytest.mark.parametrize("audit_mode", ["full", "selective"])
@pytest.mark.parametrize("repair_mode", ["unresolved", "wrong_anchor", "missing", "duplicate"])
def test_partial_scientific_failure_retains_verified_prose_not_protocol_errors(repair_mode, audit_mode):
    evidence = [source(content="An allene was obtained. The catalyst is recorded as ZnI -promoted.")]
    good, bad = "An allene was obtained.", "ZnI2 gave the allene in 98% yield."
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": text, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "a", "quote": evidence[0]["content"]}]}
                for text in (good, bad)]}]}
        valid = {"claim_id": "S01-p1-C01", "status": "supported", "text": ""}
        uncertain = {"claim_id": "S01-p1-C02", "status": "unresolved", "text": "",
                     "reason": "The formula and yield cannot be verified."}
        if label == "section-used-claim-check":
            return {"claims": [valid, uncertain]}
        assert label == "section-source-content-repair"
        assert "SAME material, experiment" in prompt
        assert "anchor_gaps" in prompt
        if repair_mode == "missing":
            return {"claims": []}
        if repair_mode == "duplicate":
            return {"claims": [uncertain, uncertain]}
        if repair_mode == "wrong_anchor":
            uncertain.update(status="rewritten", text=bad)
        return {"claims": [uncertain]}
    writing, generated, report = write_from_sources(
        section_id="S01", task=task(), evidence=evidence, context="", call=model, audit_mode=audit_mode)
    assert [c["claim"] for c in writing["claims"]] == [good]
    if repair_mode in {"missing", "duplicate"}:
        assert report["unresolved"] and not report["omitted"]
    else:
        assert not report["unresolved"]
        assert report["omitted"][0]["reason"] == "source_support_unconfirmed"
        assert report["omitted"][0]["proposed_claim"]["claim"] == bad
        assert writing["section_review"]["status"] == "needs_revision"
        package, built, synthesis, plan = bundle(writing, generated, evidence)
        synthesis["sections"][0]["source_review"] = report
        SectionsService._validate_academic_bundle(
            {"tasks": [task()], "blueprint": {"schema_version": 2}}, built, synthesis, plan, package)
    assert calls.count("section-source-content-repair") == 1


@pytest.mark.parametrize("audit_mode", ["full", "selective"])
def test_partial_chapter_finishes_as_limited_evidence_and_reuses_checkpoint(tmp_path, monkeypatch, audit_mode):
    project = tmp_path / "review-projects/demo"
    stage, planning = project / "02_section_drafting", project / "01_matrix_outline"
    stage.mkdir(parents=True)
    planning.mkdir()
    evidence = [source()]
    payloads = {stage / "section_tasks.json": [task()],
                stage / "section_evidence.json": {"sections": [{"section_id": "S01", "retrieval_mode": "lexical", "hits": evidence}]},
                planning / "literature_matrix.json": {"rows": [{"paper_id": "A", "title": "Catalyst study"}]},
                planning / "section_blueprint.json": {"review_topic": "Catalysts", "schema_version": 2, "sections": [task()]}}
    for path, payload in payloads.items():
        path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pipeline.sys, "argv", [str(SCRIPT), "--review-root", str(tmp_path), "--project-id", "demo", "--api-key", "test", "--model", "test", "--audit-mode", audit_mode])
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *a: {})
    monkeypatch.setattr(pipeline, "load_blueprint_rule_pack", lambda *a: "Use evidence")
    monkeypatch.setattr(pipeline, "load_cross_study_synthesis_skill", lambda *a: "Use evidence")
    calls = []
    bad = "ZnI2 gave 99% yield."
    def model(prompt, schema, *args, label, **kwargs):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"role": "anchor_case", "claims": [
                {"text": text, "claim_kind": "reported_finding", "support_spans": [
                        first_unit_span(prompt)]}
                for text in (evidence[0]["content"], bad)]}]}
        verdict = {"claim_id": "S01-p1-C02", "status": "unresolved", "text": "",
                   "reason": "No source supports this formula or yield."}
        return {"claims": [verdict] if label == "section-source-content-repair" else [
            {"claim_id": "S01-p1-C01", "status": "supported", "text": ""}, verdict]}
    monkeypatch.setattr(pipeline, "call_structured_llm", model)
    assert pipeline.main() == 0
    checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
    entry = checkpoint["entries"]["S01"]
    assert entry["output"]["generation_mode"] == "limited_evidence"
    assert "ZnI2" not in entry["output"]["draft_md"]
    assert "90%" in entry["output"]["draft_md"]
    assert entry["synthesis"]["source_review"]["omitted"][0]["disposition"] == "withheld"
    package = {"sections": [{"section_id": "S01", "retrieval_mode": "lexical", "hits": evidence}], "evidence_registry": evidence}
    SectionsService._validate_academic_bundle(
        {"tasks": [task()], "blueprint": {"schema_version": 2}},
        {"sections": [entry["output"]]}, {"sections": [entry["synthesis"]]},
        {"planning_mode": "evidence_first_source_writing", "sections": [entry["writing"]]}, package)
    assert pipeline.main() == 0
    assert calls == ["section-source-writing", "section-used-claim-check", "section-source-content-repair"]


def test_verified_fact_relations_reach_first_writer_without_extra_call():
    from review_writer_core.scientific_facts import attach_fact_to_evidence, FACT_VALIDATION_VERSION, review_fingerprint
    evidence = source()
    fact = {"paper_id": evidence["paper_id"], "field_id": "quantitative_results", "value": evidence["content"],
            "support_level": "direct", "evidence_refs": [{"evidence_key": evidence["evidence_key"]}],
            "study_ownership": "own_results", "experiment_id": "experiment-B",
            "qualifiers": {"scope": "tested system only"}, "epistemic_status": "direct_source_report"}
    fact["verification"] = {"contract": FACT_VALIDATION_VERSION, "status": "supported",
                            "input_fingerprint": review_fingerprint(fact)}
    attach_fact_to_evidence(evidence, fact)
    original = deepcopy(evidence)
    projected, budget = pipeline.bounded_evidence_payload([evidence], char_budget=55_000)
    assert budget["serialized_characters"] <= 55_000
    assert "evidence_refs" not in projected[0]["fact_bindings"][0]
    calls = []
    def call(prompt, _schema, label):
        calls.append(prompt)
        return {"paragraphs": [{"text": evidence["content"],
                                "claims": [prose_claim(evidence["content"], evidence["content"])]}]}
    write_from_sources(section_id="S01", task=task(), evidence=[evidence], prompt_evidence=projected,
                       context="", call=call, audit_mode="selective")
    assert len(calls) == 1
    assert '"study_ownership": "own_results"' in calls[0]
    assert '"experiment_id": "experiment-B"' in calls[0]
    assert '"scope": "tested system only"' in calls[0]
    assert evidence == original


def test_compact_fact_view_cannot_forge_or_resurrect_invisible_support():
    from review_writer_core.scientific_facts import attach_fact_to_evidence, review_fingerprint, FACT_VALIDATION_VERSION
    a, b = source(), source(key="b", content="The catalyst loading was 5 mol%.")
    fact = {"fact_id": "multi", "paper_id": "A", "field_id": "quantitative_results",
            "value": "Catalyst A degraded 90% at a loading of 5 mol%.", "support_level": "direct",
            "study_ownership": "own_results", "experiment_id": "test-A",
            "evidence_refs": [{"evidence_key": s["evidence_key"], "source_lineage_hash": "v1"} for s in (a, b)],
            "support_spans": [{"evidence_key": s["evidence_key"], "support_excerpt": s["content"]} for s in (a, b)]}
    fact["verification"] = {"contract": FACT_VALIDATION_VERSION, "status": "supported",
                            "input_fingerprint": review_fingerprint(fact)}
    attach_fact_to_evidence(a, fact)
    full = [a, b]
    projected, _ = pipeline.bounded_evidence_payload(full, char_budget=55_000)
    class Captured(BaseException):
        pass
    def capture(rows, canonical=None):
        prompts = []
        def call(prompt, _schema, _label):
            prompts.append(json.loads(prompt[prompt.index('{"task":'):]))
            raise Captured()
        with pytest.raises(Captured):
            write_from_sources(section_id="S01", task=task(), evidence=canonical or full,
                               prompt_evidence=rows, context="", call=call)
        return [f for s in prompts[0]["sources"] for f in s["verified_facts"]]
    assert capture(projected)[0]["experiment_id"] == "test-A"
    forged = deepcopy(projected)
    forged[0]["fact_bindings"][0]["value"] = "Invented 100% yield."
    forged[0]["paper_id"] = "FOREIGN"
    assert capture(forged)[0]["value"] == fact["value"]
    assert capture(projected[:1]) == []  # Other supporting chunk omitted.
    clipped = deepcopy(projected)
    clipped[1]["content"] = "The catalyst loading"
    assert capture(clipped) == []
    stale = deepcopy(full)
    stale[1]["source_lineage_hash"] = "v2"
    assert capture(projected, stale) == []
    tampered = deepcopy(full)
    tampered[0]["fact_bindings"][0]["support_spans"][0]["support_excerpt"] = "An unsupported experiment."
    assert capture(projected, tampered) == []
    rejected = deepcopy(full)
    rejected[0]["fact_bindings"][0]["verification"]["status"] = "rejected"
    assert capture(projected, rejected) == []


def test_section_thread_and_presentation_reach_writer_and_style_does_not_reject_facts():
    from review_writer_core.section_narrative_contracts import derive_narrative_diagnostics
    calls = []
    current = {**task(), "organizing_thread": "Compare scope before performance",
               "paragraph_tasks": ["Explain substrate scope"],
               "paper_roles": [{"paper_id": "B", "presentation": "table"}]}
    def model(prompt, schema, label):
        calls.append(prompt)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": source()["content"],
                "claim_kind": "reported_finding", "support_spans": [{"evidence_key": "E001", "quote": source()["content"]}]}]}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": source()["content"]}],
                "section_review": {"status": "needs_revision", "issues": ["Explain the transition."]}}
    writing, _, _ = write_from_sources(section_id="S01", task=current, evidence=[source()], context="", call=model)
    assert current["organizing_thread"] in calls[0]
    assert '"presentation": "table"' in calls[0]
    assert len(writing["claims"]) == 1
    diagnostic = derive_narrative_diagnostics(writing)
    assert diagnostic["review_status"] == "needs_revision"
    assert diagnostic["issues"] == ["Explain the transition."]
    writing["section_review"] = {"status": "coherent", "issues": []}
    assert derive_narrative_diagnostics(writing, {"minimum_paragraphs": 20})["status"] == "complete"


def write(evidence=None, *, text=None, audit_status="supported", replacement=None, span=None, records=None,
          fact_ids=None, section_task=None):
    evidence = evidence or [source()]
    calls = []
    text = text or evidence[0]["content"]
    def call(prompt, schema, label):
        calls.append((label, prompt))
        if label == "section-source-writing":
            return {"paragraphs": [{"role": "anchor_case", "reader_takeaway": "Reported conditions matter.", "claims": [
                {"text": text, "claim_kind": "reported_finding", "support_spans": span or [{"evidence_key": "a", "quote": evidence[0]["content"]}],
                 "fact_ids": list(fact_ids or []),
                 "result_context": records or []}]}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": audit_status, "text": replacement or text,
                            "reason": "Checked source, conditions and attribution."}]}
    return (*write_from_sources(section_id="S01", task=section_task or task(), evidence=evidence, context="", call=call), calls)


def test_introduction_uses_background_problem_scope_guidance_without_extra_calls():
    from review_writer_core.stages.sections.source_writing import INTRODUCTION_GUIDANCE

    intro = {**task(), "heading": "Introduction", "section_role": "introduction"}
    writing, _, _, calls = write(section_task=intro)
    assert INTRODUCTION_GUIDANCE in calls[0][1]
    assert [label for label, _ in calls] == ["section-source-writing", "section-used-claim-check"]
    assert valid_source_claim(writing["claims"][0], {"a": source()})
    assert INTRODUCTION_GUIDANCE not in write()[-1][0][1]


def test_paragraph_and_chapter_guidance_stays_inside_existing_writing_call():
    from review_writer_core.stages.sections.source_writing import ARGUMENT_USE_GUIDANCE
    _, _, _, calls = write()
    assert ARGUMENT_USE_GUIDANCE in calls[0][1]
    assert "Cover every writeable primary paper" in calls[0][1]
    assert "compare the claims within each paragraph" in calls[0][1]
    assert "Prior work quoted in a primary paper's introduction is context" in calls[0][1]
    assert "Shared evidence alone is not duplication" in calls[1][1]
    assert '"primary_papers": ["A"]' in calls[0][1]
    assert len(calls) == 2


def test_primary_contribution_recovery_is_navigation_not_a_source_verdict():
    evidence = [{**source(), "question_ids": ["primary_contribution"]}]
    _, _, _, calls = write(evidence=evidence)
    assert "Candidate primary-paper body passage; verify that it reports this study's own result." in calls[0][1]
    assert len(calls) == 2


def test_citation_display_grouping_preserves_two_audited_claims_and_api_binding():
    evidence = [source(content="Catalyst A was studied. A scale-up experiment was reported.")]
    sentences = ["Catalyst A was studied.", "A scale-up experiment was reported."]
    def call(prompt, schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"role": "anchor_case", "claims": [
                {"text": sentence, "claim_kind": "reported_finding",
                 "support_spans": [{"evidence_key": "E001", "quote": sentence}]}
                for sentence in sentences]}]}
        return {"claims": [{"claim_id": f"S01-p1-C{i:02d}", "status": "supported", "text": sentence}
                           for i, sentence in enumerate(sentences, 1)]}
    writing, generated, _ = write_from_sources(section_id="S01", task=task(), evidence=evidence, context="", call=call)
    package, built, synthesis, plan = bundle(writing, generated, evidence)
    paragraph = built["sections"][0]["paragraphs"][0]
    assert paragraph["text"] == " ".join(sentences) + " [1]"
    assert len(paragraph["claim_realizations"]) == 2
    assert [c["claim"] for c in writing["claims"]] == sentences
    SectionsService._validate_academic_bundle({"tasks": [task()], "blueprint": {"schema_version": 2}},
                                             built, synthesis, plan, package)


def test_short_source_id_maps_quotes_and_results_to_persistent_identity():
    row = source()
    writing, _, _, calls = write(span=[{"evidence_key": "E001", "quote": row["content"]}],
        records=[{"evidence_key": "E001", "object": "Catalyst A", "conditions": "25 C",
                  "result": "90%", "units": "%"}])
    claim = writing["claims"][0]
    assert claim["evidence_refs"][0]["evidence_key"] == "a"
    assert claim["result_context"][0]["evidence_key"] == "a"
    assert valid_source_claim(claim, {"a": row})
    assert '"chunk_id"' not in calls[0][1]
    assert '"evidence_key": "E001"' in calls[0][1]


def test_legacy_chunk_confusion_is_recovered_only_with_exact_shown_quote():
    row = {**source(), "chunk_id": "chk_abc123"}
    writing, _, report, _ = write([row], span=[{"evidence_key": "sha256:abc123", "quote": row["content"]}])
    assert len(writing["claims"]) == 1
    assert report["binding_repairs"][0]["method"] == "unique_chunk_and_exact_quote"
    assert valid_source_claim(writing["claims"][0], {"a": row})
    for evidence, span in [
        ([row, {**row, "paper_id": "B", "evidence_key": "b"}],
         {"evidence_key": "sha256:abc123", "quote": row["content"]}),
        ([row], {"evidence_key": "sha256:abc123", "quote": "Catalyst A degraded 99%."}),
        ([row], {"evidence_key": "invented", "quote": row["content"]}),
    ]:
        writing, _, report, calls = write(evidence, span=[span])
        assert not writing["claims"]
        assert report["omitted"][0]["binding_reason"]
        assert report["omitted"][0]["proposed_claim"]
        assert [label for label, _ in calls] == ["section-source-writing", "section-source-mapping-repair"]


def test_unicode_cleanup_preserves_exact_source_quote_before_binding():
    row = source(content="The product was an \x01-allenol.")
    response = {"text": row["content"], "support_spans": [{"evidence_key": "a", "quote": row["content"]}]}
    cleaned = pipeline.repair_model_unicode(response)
    assert cleaned["support_spans"][0]["quote"] == row["content"]
    assert "\x01" not in cleaned["text"]
    from review_writer_core.stages.sections.source_writing import resolve_spans
    assert resolve_spans(cleaned["support_spans"], {"a": row})


def test_hidden_source_text_is_not_recovered_from_full_registry():
    from review_writer_core.stages.sections.source_writing import bind_model_sources
    row = source()
    shown = {"a": {**row, "content": "Catalyst A degraded"}}
    _, reason, _ = bind_model_sources({"support_spans": [{"evidence_key": "E001", "quote": row["content"]}]}, shown, {"E001": "a"})
    assert reason == "quote_not_in_shown_source"


def test_resume_retries_old_binding_failures_without_invalidating_clean_sections():
    writing, generated, report, _ = write()
    package, built, _, _ = bundle(writing, generated, [source()])
    entry = {"output": {**built["sections"][0], "draft_md": "Supported prose"},
             "writing": writing, "synthesis": {"source_review": report}}
    args = ([task()], {"S01": package["sections"][0]})
    assert "S01" in reusable_section_entries({"S01": entry}, *args)[0]
    report["omitted"] = [{"reason": "missing_or_invalid_source_span"}]
    assert "S01" in reusable_section_entries({"S01": entry}, *args)[0]
    report.pop("binding_contract")
    retained, rejected = reusable_section_entries({"S01": entry}, *args)
    assert not retained and "legacy source-binding" in rejected["S01"]


def bundle(writing, generated, evidence):
    overview, paragraphs, checks, reviews = pipeline.validate_and_realize_section(
        section_id="S01", generated=generated, writing_section=writing, evidence=evidence, citation_map={"A": 1, "B": 2})
    package = {"sections": [{"section_id": "S01", "retrieval_mode": "lexical", "hits": evidence}], "evidence_registry": evidence}
    built = {"sections": [{"section_id": "S01", "paragraphs": paragraphs, "overview": overview}]}
    return package, built, {"sections": [{"section_id": "S01", "components": []}]}, {"planning_mode": "evidence_first_source_writing", "sections": [writing]}


def test_no_fact_cards_write_check_publish_and_resume():
    evidence = [source()]
    writing, generated, report, calls = write(evidence)
    assert [c[0] for c in calls] == ["section-source-writing", "section-used-claim-check"]
    assert writing["claims"][0]["fact_ids"] == []
    assert valid_source_claim(writing["claims"][0], {"a": evidence[0]})
    package, built, synthesis, plan = bundle(writing, generated, evidence)
    payload = {"tasks": [task()], "blueprint": {"schema_version": 2}}
    SectionsService._validate_academic_bundle(payload, built, synthesis, plan, package)
    output = {**built["sections"][0], "draft_md": "Supported prose"}
    saved = {"S01": {"output": output, "synthesis": synthesis["sections"][0], "writing": writing}}
    retained, rejected = reusable_section_entries(saved, [task()], {"S01": package["sections"][0]})
    assert set(retained) == {"S01"} and not rejected
    for mutate in (lambda: evidence[0].update(source_lineage_hash="v2"),
                   lambda: evidence[0].update(content="Changed source")):
        mutate()
        assert not valid_source_claim(writing["claims"][0], {"a": evidence[0]})
        with pytest.raises(WorkflowValidationError, match="source passage"):
            SectionsService._validate_academic_bundle(payload, built, synthesis, plan, package)


def test_existing_unrelated_fact_card_does_not_require_fact_selection():
    evidence = [source()]
    evidence[0]["fact_bindings"] = [{"fact_id": "old", "value": "Different experiment"}]
    writing, generated, report, calls = write(evidence)
    package, built, synthesis, plan = bundle(writing, generated, evidence)
    SectionsService._validate_academic_bundle({"tasks": [task()], "blueprint": {"schema_version": 2}}, built, synthesis, plan, package)
    assert 'Different experiment' not in calls[0][1]


def test_verified_fact_guides_writing_and_is_bound_only_with_its_source_span():
    from review_writer_core.scientific_facts import attach_fact_to_evidence

    evidence = [source()]
    fact = {
        "fact_id": "F-A",
        "paper_id": "A",
        "field_id": "quantitative_results",
        "value": "Catalyst A degraded 90% of the pollutant at 25 C after 60 minutes.",
        "support_level": "direct",
        "assertion_ceiling": "direct_source_report",
        "evidence_refs": [{"evidence_key": "a"}],
    }
    attach_fact_to_evidence(evidence[0], fact)
    writing, _, _, calls = write(evidence, fact_ids=["F-A"])
    claim = writing["claims"][0]
    assert "F-A" in calls[0][1]
    assert claim["fact_ids"] == ["F-A"]
    assert claim["fact_binding_status"] == "verified_fact_guided"
    assert valid_source_claim(claim, {"a": evidence[0]})


def test_unregistered_fact_selection_is_dropped_without_discarding_supported_prose():
    evidence = [source()]
    writing, _, report, _ = write(evidence, fact_ids=["F-FOREIGN"])
    assert writing["claims"][0]["fact_ids"] == []
    assert writing["claims"][0]["fact_binding_status"] == CONTRACT
    assert not report["omitted"]


def test_audited_abstract_report_with_optional_background_fact_can_publish():
    from review_writer_core.scientific_facts import attach_fact_to_evidence, fact_claim_issues
    content = "The abstract reports Catalyst A degraded 90% of the pollutant."
    row = source(content=content)
    row.update(source_channel="abstract", assertion_ceiling="abstract_report_only",
               support_level="abstract_limited", match_type="abstract_only", claim_eligible=False)
    fact = {"fact_id": "F-ABSTRACT", "paper_id": "A", "field_id": "contribution",
            "value": content, "support_level": "abstract_limited", "source_channel": "abstract",
            "assertion_ceiling": "abstract_report_only", "evidence_refs": [{"evidence_key": "a"}]}
    attach_fact_to_evidence(row, fact)
    assert "abstract_detail_requires_full_text" in fact_claim_issues(content, [fact], claim_kind="reported_finding")
    writing, generated, _, _ = write([row], fact_ids=[fact["fact_id"]])
    assert writing["claims"][0]["assertion_ceiling"] == "abstract_report_only"
    package, built, synthesis, plan = bundle(writing, generated, [row])
    payload = {"tasks": [task()], "blueprint": {"schema_version": 2}}
    SectionsService._validate_academic_bundle(payload, built, synthesis, plan, package)
    built["sections"][0]["paragraphs"][0]["claim_realizations"][0]["text"] = "Catalyst A caused 100% degradation."
    with pytest.raises(WorkflowValidationError, match="wording has changed"):
        SectionsService._validate_academic_bundle(payload, built, synthesis, plan, package)


@pytest.mark.parametrize("span", [[{"evidence_key": "foreign", "quote": "Invented"}],
                                  [{"evidence_key": "a", "quote": "Made-up quote"}],
                                  [{"evidence_key": "a", "quote": source()["content"]}, {"evidence_key": "x", "quote": "x"}]])
def test_bad_source_selection_is_omitted_after_one_unsuccessful_local_repair(span):
    writing, _, report, calls = write(span=span)
    assert not writing["claims"] and report["omitted"]
    assert [label for label, _ in calls] == ["section-source-writing", "section-source-mapping-repair"]


def test_unsupported_ranking_is_narrowed_once_without_extracting_all_paper_facts():
    evidence = [source(), source("b", "B", "Catalyst B degraded 95% at 40 C after 30 minutes.")]
    safe = "The studies used different temperatures and reaction times."
    spans = [{"evidence_key": r["evidence_key"], "quote": r["content"]} for r in evidence]
    writing, _, report, calls = write(evidence, text="Catalyst B is superior to A.", audit_status="narrowed", replacement=safe, span=spans)
    assert writing["claims"][0]["claim"] == safe
    assert report["narrowed"] and not report["omitted"] and len(calls) == 2
    assert 'reader_takeaway' not in writing['paragraphs'][0]
    assert 'evidence_uses' not in writing['paragraphs'][0]


def test_audit_cannot_introduce_unquoted_numbers_or_new_sources():
    writing, _, report, _ = write(audit_status="narrowed", replacement="The yield was 99%.")
    assert not writing["claims"] and report["unresolved"] and not report["omitted"]


def test_incomplete_audit_preserves_candidates_and_resumes_only_unchecked_claims():
    saved, calls = {}, []
    sentences = ["Catalyst A was studied.", "Scale-up was reported."]
    evidence = [source(content=" ".join(sentences))]
    def persist(state):
        saved.clear()
        saved.update(deepcopy(state))
    def call(prompt, schema, label):
        calls.append((label, prompt))
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": text, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": text}]} for text in sentences]}]}
        cid = "S01-p1-C01" if len(calls) == 2 else "S01-p1-C02"
        return {"claims": [{"claim_id": cid, "status": "supported", "text": "", "reason": ""}]}
    first, _, report = write_from_sources(section_id="S01", task=task(), evidence=evidence, context="", call=call, save_state=persist)
    assert len(first["claims"]) == 1
    assert report["unresolved"][0]["proposed_claim"]["claim"] == sentences[1]
    assert not report["omitted"]
    second, _, report = write_from_sources(section_id="S01", task=task(), evidence=evidence, context="", call=call,
                                           resume_state=deepcopy(saved), save_state=persist)
    assert len(second["claims"]) == 2 and not report["unresolved"]
    assert [label for label, _ in calls].count("section-source-writing") == 1
    assert '"claim_id": "S01-p1-C01"' not in calls[-1][1]


def test_invalid_replacement_rechecks_original_once_instead_of_deleting_it():
    calls = []
    def call(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": source()["content"], "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": source()["content"]}]}]}]}
        status, text = ("narrowed", "The yield was 99%.") if label == "section-used-claim-check" else ("supported", "")
        return {"claims": [{"claim_id": "S01-p1-C01", "status": status, "text": text, "reason": ""}]}
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=call)
    assert writing["claims"][0]["claim"] == source()["content"]
    assert not report["omitted"] and not report["unresolved"]
    assert calls.count("section-source-content-repair") == 1


def test_deferred_content_repair_resumes_without_discarding_the_paid_result():
    saved, repair_prompts = {}, []

    def save(value):
        saved.clear()
        saved.update(deepcopy(value))

    def first_call(prompt, _schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [prose_claim(source()["content"], source()["content"])]}]}
        if label == "section-used-claim-check":
            return {"claims": [{"claim_id": "S01-p1-C01", "status": "narrowed",
                                "text": "The yield was 99%.", "reason": "Check result."}]}
        assert label == "section-source-content-repair"
        repair_prompts.append(prompt)
        raise DeferredModelCall("model-child")

    arguments = dict(section_id="S01", task=task(), evidence=[source()], context="", save_state=save)
    with pytest.raises(DeferredModelCall):
        write_from_sources(**arguments, call=first_call)
    assert saved["content_repair_attempted"] and saved["content_repair_pending"]

    def resumed_call(prompt, _schema, label):
        assert label == "section-source-content-repair"
        repair_prompts.append(prompt)
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}

    writing, _, _ = write_from_sources(**arguments, call=resumed_call, resume_state=saved)
    assert len(repair_prompts) == 2 and repair_prompts[0] == repair_prompts[1]
    assert writing["claims"][0]["source_verification"]["status"] == "supported"
    assert not saved.get("content_repair_pending")


def test_result_records_are_only_kept_for_actual_supported_claims():
    records = [{"evidence_key": "a", "object": "Catalyst A", "conditions": "25 C; 60 minutes", "result": "90%", "units": "%"}]
    writing, _, report, calls = write(records=records)
    assert writing["claims"][0]["result_context"] == records
    assert "Table record problems are separate from prose support" in calls[1][1]
    writing, _, report, _ = write(records=records, audit_status="unsupported")
    assert not writing["claims"]


def test_checked_text_tampering_is_rejected():
    writing, generated, _, _ = write()
    generated["paragraphs"][0]["claim_realizations"][0]["text"] = "Unverified conclusion."
    with pytest.raises(RuntimeError, match="source support changed"):
        bundle(writing, generated, [source()])


def test_prompt_compaction_does_not_change_source_lineage():
    full = source(content=source()["content"] + " Additional full-text context.")
    shown = {**full, "content": source()["content"]}
    def call(prompt, schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": shown["content"], "support_spans": [{"evidence_key": "a", "quote": shown["content"]}]}]}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": shown["content"]}]}
    writing, _, _ = write_from_sources(section_id="S01", task=task(), evidence=[full], prompt_evidence=[shown], context="", call=call)
    assert valid_source_claim(writing["claims"][0], {"a": full})


def test_abstract_passage_is_addressable_without_becoming_fulltext_evidence():
    evidence = [source(content="The study examines pollutant degradation.")]
    evidence[0].update(claim_eligible=False, match_type="abstract_only", support_level="abstract_limited",
                       assertion_ceiling="abstract_report_only", source_channel="abstract")
    writing, generated, _, _ = write(evidence)
    assert writing["claims"][0]["assertion_ceiling"] == "abstract_report_only"
    package, built, synthesis, plan = bundle(writing, generated, evidence)
    SectionsService._validate_academic_bundle({"tasks": [task()], "blueprint": {"schema_version": 2}}, built, synthesis, plan, package)
    assert pipeline.missing_primary_papers(["A"], built["sections"][0]["paragraphs"], require_evidence=True, source_evidence=evidence) == ["A"]


def test_unchecked_neighbor_cannot_support_source_claim():
    evidence = [source()]
    evidence[0].update(claim_eligible=False, match_type="neighbor_context")
    writing, _, report, calls = write(evidence)
    assert not writing["claims"] and report["omitted"] and not calls


def test_overview_execution_uses_checked_prose_and_condition_records():
    from review_writer_core.section_narrative_contracts import build_argument_execution
    writing, generated, _, _ = write()
    _, built, _, plan = bundle(writing, generated, [source()])
    blueprint = {"sections": [{**task(), "title": "Catalysts"}]}
    result = build_argument_execution(blueprint, plan, built, {"rows": []})
    claim = result["sections"][0]["claims"][0]
    assert claim["binding_level"] == "source_passage" and claim["evidence_refs"][0]["quote"]
    writing["claims"][0]["source_verification"]["input_fingerprint"] = "stale"
    result = build_argument_execution(blueprint, plan, built, {"rows": []})
    assert not result["sections"][0]["claims"]


@pytest.mark.parametrize("audit_mode,fail_review", [("full", False), ("selective", False), ("full", True)])
def test_real_generator_without_fact_cards_finishes_and_reuses_checkpoint(tmp_path, monkeypatch, audit_mode, fail_review):
    project = tmp_path / "review-projects/demo"
    stage, planning = project / "02_section_drafting", project / "01_matrix_outline"
    stage.mkdir(parents=True); planning.mkdir()
    evidence = [source()]
    payloads = {stage / "section_tasks.json": [task()],
                stage / "section_evidence.json": {"sections": [{"section_id": "S01", "retrieval_mode": "lexical", "hits": evidence}]},
                planning / "literature_matrix.json": {"rows": [{"paper_id": "A", "title": "Catalyst study"}]},
                planning / "section_blueprint.json": {"review_topic": "Catalysts", "schema_version": 2, "sections": [task()]}}
    for path, payload in payloads.items():
        path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pipeline.sys, "argv", [str(SCRIPT), "--review-root", str(tmp_path), "--project-id", "demo", "--api-key", "test", "--model", "test", "--audit-mode", audit_mode])
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *a: {})
    monkeypatch.setattr(pipeline, "load_blueprint_rule_pack", lambda *a: "Use evidence")
    monkeypatch.setattr(pipeline, "load_cross_study_synthesis_skill", lambda *a: "Use evidence")
    calls = []
    def model(prompt, schema, *args, label, **kwargs):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"role": "anchor_case", "claims": [{"text": evidence[0]["content"], "support_spans": [first_unit_span(prompt)]}]}]}
        if fail_review and calls.count("section-used-claim-check") == 1:
            raise RuntimeError("Provider temporarily unavailable")
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": evidence[0]["content"]}]}
    monkeypatch.setattr(pipeline, "call_structured_llm", model)
    if fail_review:
        with pytest.raises(SystemExit, match="Retry the job"):
            pipeline.main()
        pending = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
        assert pending["authoring_states"]["S01"]["state"]["proposed"]
        assert pending["authoring_states"]["S01"]["state"]["repair_attempted"]
    assert pipeline.main() == 0
    checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
    assert checkpoint["entries"]["S01"]["output"]["paragraphs"]
    assert checkpoint["entries"]["S01"]["writing"]["evidence_mode"] == CONTRACT
    assert pipeline.main() == 0
    assert len(calls) == (2 if audit_mode == "full" else 1) + int(fail_review)
    assert calls.count("section-source-writing") == 1


def test_compact_audit_retains_exact_prose_and_bindings_but_not_missing_or_duplicate_verdicts():
    for verdicts, expected in [([{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}], 1),
                               ([], 0),
                               ([{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}] * 2, 0),
                               ([{"claim_id": "foreign", "status": "supported", "text": "", "reason": ""}], 0)]:
        evidence = [source()]
        def call(prompt, schema, label):
            if label == "section-source-writing":
                return {"paragraphs": [{"role": "anchor_case", "reader_takeaway": "Result", "claims": [
                    {"text": evidence[0]["content"], "claim_kind": "reported_finding",
                     "support_spans": [{"evidence_key": "E001", "quote": evidence[0]["content"]}],
                     "fact_ids": [], "result_context": []}]}]}
            return {"claims": verdicts}
        writing, realized, report = write_from_sources(section_id="S01", task=task(), evidence=evidence, context="", call=call)
        assert len(writing["claims"]) == expected
        if expected:
            assert writing["claims"][0]["claim"] == evidence[0]["content"]
            assert valid_source_claim(writing["claims"][0], {"a": evidence[0]})


def test_parallel_pipeline_preserves_outline_order_checkpoints_and_citations(tmp_path, monkeypatch):
    from threading import Barrier
    stage = tmp_path / "review-projects" / "test" / "02_section_drafting"
    planning = stage.parent / "01_matrix_outline"
    stage.mkdir(parents=True)
    planning.mkdir()
    tasks = [{**task(), "section_id": sid, "heading": sid, "allowed_papers": ["A"]} for sid in ("S01", "S02")]
    sources = [{"section_id": t["section_id"], "retrieval_mode": "lexical", "hits": [source()]} for t in tasks]
    for path, data in {stage / "section_tasks.json": [*tasks, {"section_id": "S03", "section_role": "conclusion"}],
        stage / "section_evidence.json": {"sections": sources},
        planning / "literature_matrix.json": {"rows": [{"paper_id": "A", "title": "Study"}]},
        planning / "section_blueprint.json": {"sections": tasks}}.items():
        path.write_text(json.dumps(data), encoding="utf-8")
    barrier = Barrier(2)
    def model(prompt, schema, *args, label, **kwargs):
        if label == "section-source-writing":
            barrier.wait(timeout=4)
            return {"paragraphs": [{"role": "anchor_case", "reader_takeaway": "Result", "claims": [
                {"text": source()["content"], "claim_kind": "reported_finding",
                 "support_spans": [first_unit_span(prompt)],
                 "fact_ids": [], "result_context": []}]}]}
        data = json.loads(prompt[prompt.index('{"claims":'):])
        return {"claims": [{"claim_id": c["claim_id"], "status": "supported", "text": "", "reason": ""} for c in data["claims"]]}
    monkeypatch.setattr(pipeline, "call_structured_llm", model)
    monkeypatch.setattr(pipeline, "load_dotenv", lambda _: {})
    monkeypatch.setattr(pipeline, "load_blueprint_rule_pack", lambda *_: "")
    monkeypatch.setattr(pipeline, "load_cross_study_synthesis_skill", lambda: "")
    monkeypatch.setattr(pipeline.sys, "argv", [str(SCRIPT), "--review-root", str(tmp_path), "--project-id", "test", "--api-key", "test"])
    assert pipeline.main() == 0
    data = json.loads((stage / "section_drafts.json").read_text(encoding="utf-8"))
    assert [s["section_id"] for s in data["sections"]] == ["S01", "S02"]
    for section in data["sections"]:
        assert "[1]" in section["draft_md"]
    checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
    assert set(checkpoint["entries"]) == {"S01", "S02"}
    monkeypatch.setattr(pipeline, "call_structured_llm", lambda *_a, **_k: pytest.fail("Completed chapters must not be regenerated"))
    assert pipeline.main() == 0


def selective_model(evidence, paragraphs, *, verdict=None):
    calls = []
    def model(prompt, schema, label):
        calls.append((label, prompt))
        if label == "section-source-writing":
            return {"paragraphs": paragraphs}
        if label == "section-source-mapping-repair":
            return {"repairs": []}
        data = json.loads(prompt[prompt.index('{"claims":'):])
        if verdict:
            return verdict(data)
        return {"claims": [{"claim_id": c["claim_id"], "status": "supported", "text": "", "reason": ""}
                           for c in data["claims"]]}
    return model, calls


def prose_claim(text, quote, *, reasons=None, kind="reported_finding"):
    return {"text": text, "claim_kind": kind, "support_spans": [{"evidence_key": "E001", "quote": quote}],
            "review_reasons": reasons or [], "fact_ids": [], "result_context": []}


def test_continuous_prose_selective_checks_publish_resume_and_argument_projection():
    evidence = [source()]
    text = "After 60 minutes at 25 C, Catalyst A achieved 90% pollutant degradation."
    paragraph = {"text": "We next compare the methods. " + text,
                 "claims": [prose_claim(text, evidence[0]["content"])]}
    model, calls = selective_model(evidence, [paragraph])
    writing, generated, report = write_from_sources(section_id="S01", task=task(), evidence=evidence,
        context="", call=model, audit_mode="selective")
    assert len(calls) == 1 and report["checked_claim_count"] == 0
    claim = writing["claims"][0]
    assert claim["source_verification"]["status"] == "program_checked"
    assert valid_source_claim(claim, {"a": evidence[0]})
    package, built, synthesis, plan = bundle(writing, generated, evidence)
    output = built["sections"][0]
    assert output["paragraphs"][0]["text"] == paragraph["text"] + " [1]"
    SectionsService._validate_academic_bundle({"tasks": [task()], "blueprint": {"schema_version": 2}},
                                             built, synthesis, plan, package)
    entry = {"output": {**output, "draft_md": paragraph["text"]}, "writing": writing, "synthesis": synthesis["sections"][0]}
    assert "S01" in reusable_section_entries({"S01": entry}, [task()], {"S01": package["sections"][0]})[0]
    from review_writer_core.section_narrative_contracts import build_argument_execution
    execution = build_argument_execution({"sections": [task()]}, plan, built, {"rows": []})
    assert execution["sections"][0]["claims"][0]["source_verification"]["status"] == "program_checked"
    output["paragraphs"][0]["text"] += " This proves a universal mechanism."
    with pytest.raises(WorkflowValidationError, match="mapping"):
        SectionsService._validate_academic_bundle({"tasks": [task()], "blueprint": {"schema_version": 2}},
                                                 built, synthesis, plan, package)
    assert not reusable_section_entries({"S01": entry}, [task()], {"S01": package["sections"][0]})[0]


def test_selective_review_sends_only_concrete_uncertainties():
    evidence = [source()]
    ordinary = prose_claim("Catalyst A was studied at 25 C.", evidence[0]["content"])
    uncertain = prose_claim("Catalyst A achieved 90% degradation.", evidence[0]["content"], reasons=["Study ownership unclear in this passage."])
    model, calls = selective_model(evidence, [{"claims": [ordinary, uncertain]}])
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=evidence,
        context="", call=model, audit_mode="selective")
    assert report["checked_claim_count"] == 1
    data = json.loads(calls[1][1][calls[1][1].index('{"claims":'):])
    assert [c["claim_id"] for c in data["claims"]] == ["S01-p1-C02"]
    assert [c["source_verification"]["status"] for c in writing["claims"]] == ["program_checked", "supported"]


def test_review_reason_order_keeps_delegated_request_stable():
    sentences = [f"Catalyst A was studied under condition {index}." for index in range(8)]
    evidence = [source(content=" ".join(sentences))]
    paragraphs = [{"claims": [prose_claim(sentence, sentence, reasons=["Check attribution."])
                              for sentence in sentences]}]
    model, calls = selective_model(evidence, paragraphs)
    write_from_sources(section_id="S01", task=task(), evidence=evidence,
                       context="", call=model, audit_mode="selective")
    prompts = [prompt for label, prompt in calls if label == "section-used-claim-check"]
    assert len(prompts) == 1
    payload = json.loads(prompts[0][prompts[0].index('{"claims":'):])
    claim_ids = [claim["claim_id"] for claim in payload["claims"]]
    assert len(claim_ids) == len(sentences)
    assert list(payload["review_reasons"]) == sorted(claim_ids)


def test_deferred_audit_resumes_the_same_paid_request():
    evidence = [source()]
    paragraph = {"claims": [prose_claim(evidence[0]["content"], evidence[0]["content"])]}
    saved, audit_prompts = {}, []

    def save(value):
        saved.clear()
        saved.update(deepcopy(value))

    def first_call(prompt, _schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [paragraph]}
        audit_prompts.append(prompt)
        raise DeferredModelCall("model-child")

    arguments = dict(section_id="S01", task=task(), evidence=evidence,
                     context="", audit_mode="full", save_state=save)
    with pytest.raises(DeferredModelCall):
        write_from_sources(**arguments, call=first_call)
    assert saved["proposed"]["paragraphs"] == [paragraph]
    assert saved["repair_attempted"] and saved["audit_pending"]

    def resumed_call(prompt, _schema, label):
        assert label == "section-used-claim-check"
        audit_prompts.append(prompt)
        payload = json.loads(prompt[prompt.index('{"claims":'):])
        return {"claims": [{"claim_id": claim["claim_id"], "status": "supported",
                            "text": "", "reason": ""} for claim in payload["claims"]]}

    writing, _, _ = write_from_sources(**arguments, call=resumed_call, resume_state=saved)
    assert len(audit_prompts) == 2 and audit_prompts[0] == audit_prompts[1]
    assert writing["claims"][0]["source_verification"]["status"] == "supported"
    assert not saved.get("audit_pending")


@pytest.mark.parametrize("prefix", ["The mechanism is universal. ", "We next compare the methods at 99 C. "])
def test_unmapped_science_is_not_allowed_as_a_transition(prefix):
    text = "Catalyst A was studied at 25 C."
    def model(prompt, schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"text": prefix + text, "claims": [prose_claim(text, source()["content"])]}]}
        assert label == "section-used-claim-check"
        assert "discourse_span_requires_source_review" in prompt
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "narrowed", "text": text,
                            "reason": "The prefixed assertion is not supported."}]}
    writing, generated, report = write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=model, audit_mode="selective")
    assert report["checked_claim_count"] == 1
    assert prefix.strip() not in bundle(writing, generated, [source()])[1]["sections"][0]["paragraphs"][0]["text"]


def test_ambiguous_span_is_not_silently_bound_to_first_occurrence():
    from review_writer_core.stages.sections.authoring import prose_layout
    assert prose_layout("Same sentence. Same sentence.", [{"claim_id": "C1", "claim": "Same sentence."}])[1]


def test_style_failure_keeps_original_and_does_not_repeat_on_resume():
    text = "This method provides a practical approach to the transformation of a wide range of starting materials under the reported conditions while retaining the experimentally demonstrated limitations."
    evidence = [source(content=text)]
    model, calls = selective_model(evidence, [{"text": text, "claims": [prose_claim(text, text)]}],
        verdict=lambda data: (_ for _ in ()).throw(RuntimeError("Provider unavailable")))
    saved = {}
    def save(value):
        saved.clear()
        saved.update(deepcopy(value))
    args = dict(section_id="S01", task=task(), evidence=evidence, context="", call=model, audit_mode="selective", save_state=save)
    writing, _, report = write_from_sources(**args)
    assert writing["claims"][0]["claim"] == text and not report["style_repair_attempted"]
    assert writing["claims"][0]["source_verification"]["status"] == "program_checked"
    assert len(calls) == 1
    write_from_sources(**args, resume_state=saved)
    assert len(calls) == 1


def test_scientific_review_failure_resumes_draft_without_repeating_generation():
    evidence = [source()]
    text = "Catalyst A achieved 90% pollutant degradation."
    model, calls = selective_model(evidence, [{"claims": [prose_claim(text, source()["content"], reasons=["Attribution uncertain."])]}],
        verdict=lambda data: (_ for _ in ()).throw(RuntimeError("Provider unavailable")))
    saved = {}
    def save(value):
        saved.clear(); saved.update(deepcopy(value))
    args = dict(section_id="S01", task=task(), evidence=evidence, context="", audit_mode="selective", save_state=save)
    with pytest.raises(RuntimeError, match="unavailable"):
        write_from_sources(**args, call=model)
    retry, retry_calls = selective_model(evidence, [])
    writing, _, _ = write_from_sources(**args, call=retry, resume_state=saved)
    assert [c[0] for c in retry_calls] == ["section-used-claim-check"]
    assert writing["claims"][0]["source_verification"]["status"] == "supported"


def test_program_check_rejects_changed_text_and_forged_check_scope():
    text = "Catalyst A was studied at 25 C."
    model, _ = selective_model([source()], [{"claims": [prose_claim(text, source()["content"])]}])
    writing, _, _ = write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=model, audit_mode="selective")
    claim = writing["claims"][0]
    assert not valid_source_claim(claim, {"a": source()}, text="Catalyst A was studied at 100 C.")
    claim["source_verification"]["review_reasons"] = ["Unresolved data conflict"]
    assert not valid_source_claim(claim, {"a": source()})


def test_program_checked_comparison_records_are_not_lost_or_mislabeled():
    from review_writer_core.publication_tables import _source_comparison_cells
    from review_writer_core.section_narrative_contracts import derive_narrative_diagnostics
    text = "Catalyst A achieved 90% degradation at 25 C."
    claim = prose_claim(text, source()["content"])
    claim["result_context"] = [{"evidence_key": "E001", "object": "Catalyst A", "conditions": "25 C",
                               "result": "90%", "units": "%"}]
    model, _ = selective_model([source()], [{"text": text, "claims": [claim]}])
    writing, generated, _ = write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=model, audit_mode="selective")
    _, built, _, _ = bundle(writing, generated, [source()])
    cells = _source_comparison_cells(built["sections"][0], ["A"])
    assert {c["field_id"] for c in cells} == {"object_input", "method_conditions", "quantitative_results"}
    diagnostic = derive_narrative_diagnostics(writing)
    assert diagnostic["status"] == "not_reviewed" and diagnostic["missing_requirements"] == []


def test_bad_optional_style_edit_keeps_original_not_invented_numbers():
    text = "This method provides a practical approach to the transformation of a wide range of starting materials under the reported conditions while retaining the experimentally demonstrated limitations."
    model, _ = selective_model([source(content=text)], [{"text": text, "claims": [prose_claim(text, text)]}],
        verdict=lambda data: {"claims": [{"claim_id": "S01-p1-C01", "status": "rewritten", "text": "Yield reached 99%.", "reason": "Shorter."}]})
    writing, _, _ = write_from_sources(section_id="S01", task=task(), evidence=[source(content=text)], context="", call=model, audit_mode="selective")
    assert writing["claims"][0]["claim"] == text
    assert writing["claims"][0]["source_verification"]["status"] == "program_checked"


def test_continuous_prose_span_is_not_truncated_at_old_sentence_limit():
    text = " ".join(f"The method addresses the reported scope of experimental category {i}." for i in range(50))
    evidence = [source(content=text)]
    model, _ = selective_model(evidence, [{"text": text, "claims": [prose_claim(text, text)]}])
    writing, generated, _ = write_from_sources(section_id="S01", task=task(), evidence=evidence, context="", call=model)
    _, built, _, _ = bundle(writing, generated, evidence)
    assert built["sections"][0]["paragraphs"][0]["claim_realizations"][0]["text"] == text


def test_successful_style_repair_replays_exact_result_without_new_call():
    original = "This method provides a practical approach to the transformation of a wide range of starting materials under the reported conditions while retaining the experimentally demonstrated limitations."
    revised = "The demonstrated scope supports this transformation within its reported experimental limits."
    model, calls = selective_model([source(content=original)], [{"text": original, "claims": [prose_claim(original, original)]}],
        verdict=lambda data: {"claims": [{"claim_id": "S01-p1-C01", "status": "rewritten", "text": revised, "reason": "Rephrased source overlap."}]})
    snapshots = []
    args = dict(section_id="S01", task=task(), evidence=[source(content=original)], context="", call=model,
                audit_mode="full", save_state=lambda value: snapshots.append(deepcopy(value)))
    first = write_from_sources(**args)
    resumed = write_from_sources(**args, resume_state=snapshots[-1])
    assert first == resumed and len(calls) == 2
    assert first[0]["claims"][0]["claim"] == revised
    assert valid_source_claim(first[0]["claims"][0], {"a": source(content=original)})


def test_local_mapping_repair_recovers_prose_and_quote_and_is_checkpointed():
    evidence = [source()]
    text = "After 60 minutes at 25 C, Catalyst A achieved 90% pollutant degradation."
    broken = {"text": text, "claims": [prose_claim(text, "Incorrect copied quote")]}
    repaired = {"text": text, "claims": [prose_claim(text, evidence[0]["content"])]}
    saved, calls = {}, []
    def save(state):
        saved.clear()
        saved.update(deepcopy(state))
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [broken]}
        if label == "section-source-mapping-repair":
            from review_writer_core.stages.sections.source_writing import fingerprint
            return {"claim_repairs": [{"paragraph_index": 0, "claim_index": 0,
                "input_fingerprint": fingerprint(broken["claims"][0]),
                "support_spans": repaired["claims"][0]["support_spans"], "result_context": None}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}
    args = dict(section_id="S01", task=task(), evidence=evidence, context="", call=model, save_state=save)
    result = write_from_sources(**args)
    assert result[0]["claims"][0]["claim"] == text
    assert result[2]["omitted"] == result[2]["prose_issues"] == []
    assert valid_source_claim(result[0]["claims"][0], {"a": evidence[0]})
    assert write_from_sources(**args, resume_state=deepcopy(saved)) == result
    assert calls == ["section-source-writing", "section-source-mapping-repair", "section-used-claim-check"]


def test_deferred_mapping_repair_resumes_the_same_paid_request():
    evidence = [source()]
    text = "After 60 minutes at 25 C, Catalyst A achieved 90% pollutant degradation."
    broken = {"text": text, "claims": [prose_claim(text, "Incorrect copied quote")]}
    repaired = {"text": text, "claims": [prose_claim(text, evidence[0]["content"])]}
    saved, repair_prompts = {}, []

    def save(value):
        saved.clear()
        saved.update(deepcopy(value))

    def first_call(prompt, _schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [broken]}
        assert label == "section-source-mapping-repair"
        repair_prompts.append(prompt)
        raise DeferredModelCall("model-child")

    arguments = dict(section_id="S01", task=task(), evidence=evidence, context="", save_state=save)
    with pytest.raises(DeferredModelCall):
        write_from_sources(**arguments, call=first_call)
    assert saved["mapping_repair_attempted"] and saved["mapping_repair_pending"]

    def resumed_call(prompt, _schema, label):
        if label == "section-source-mapping-repair":
            repair_prompts.append(prompt)
            from review_writer_core.stages.sections.source_writing import fingerprint
            return {"claim_repairs": [{"paragraph_index": 0, "claim_index": 0,
                "input_fingerprint": fingerprint(broken["claims"][0]),
                "support_spans": repaired["claims"][0]["support_spans"], "result_context": None}]}
        assert label == "section-used-claim-check"
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}

    writing, _, _ = write_from_sources(**arguments, call=resumed_call, resume_state=saved)
    assert len(repair_prompts) == 2 and repair_prompts[0] == repair_prompts[1]
    assert writing["claims"][0]["claim"] == text
    assert not saved.get("mapping_repair_pending")


def test_bad_table_cell_does_not_remove_supported_prose():
    writing, _, report, _ = write(records=[{"evidence_key": "a", "object": "Catalyst A", "conditions": "25 C", "result": "99%", "units": ""}])
    assert len(writing["claims"]) == 1
    assert writing["claims"][0]["result_context"] == []
    assert report["omitted"] == []
    assert report["record_issues"][0]["reason"] == "result_context_exceeds_selected_source"


def test_supported_verdict_with_redundant_rewording_keeps_original():
    writing, _, report, _ = write(replacement="Catalyst A achieved 90% degradation at 25 C in 60 minutes.")
    assert writing["claims"][0]["claim"] == source()["content"]
    assert report["omitted"] == []


def test_processing_notes_do_not_imply_shallow_content():
    from review_writer_core.section_narrative_contracts import derive_narrative_diagnostics
    result = derive_narrative_diagnostics({"paragraphs": [{}], "section_review": {
        "status": "partially_reviewed", "issues": ["Comparison was narrowed to the reported conditions."]}})
    assert result["status"] == "not_reviewed"
    assert result["missing_requirements"] == []


def test_stale_automatic_word_budget_is_recalculated_but_custom_budget_is_preserved():
    from review_writer_core.section_narrative_contracts import resolve_section_depth_contract
    section = {"section_role": "body", "primary_papers": [], "target_words": 8050,
               "depth_contract": {"target_paragraph_count": 4, "target_word_min": 6440,
                                  "target_word_max": 10062, "diagnostic_policy": "derived_not_hard_word_quota"}}
    assert resolve_section_depth_contract(section)["target_word_min"] == 0
    section["depth_contract"]["diagnostic_policy"] = "user_defined"
    assert resolve_section_depth_contract(section)["target_word_min"] == 0


@pytest.mark.parametrize("malformed", [None, {}, {"repairs": None}, {"repairs": [None]}])
def test_optional_mapping_repair_malformed_response_preserves_valid_claims(malformed):
    text = "Catalyst A was studied at 25 C."
    def model(prompt, schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"text": "A differently worded paragraph without the supplied claim span.",
                                    "claims": [prose_claim(text, source()["content"])]}]}
        if label == "section-source-mapping-repair":
            return malformed
        pytest.fail("No scientific uncertainty was introduced into retained source-bound text")
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=[source()], context="", call=model, audit_mode="selective")
    assert writing["claims"][0]["claim"] == text
    assert report["prose_issues"]


def test_missing_optional_table_record_does_not_rewrite_supported_prose():
    text = source()["content"]
    original = {"text": text, "claims": [prose_claim(text, text)]}
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        assert label == "section-source-writing"
        return {"paragraphs": [original]}
    writing, _, report = write_from_sources(section_id="S01", task={**task(), "paper_roles": [
        {"paper_id": "A", "presentation": "table"}]}, evidence=[source()], context="", call=model, audit_mode="selective")
    assert not writing["claims"][0]["result_context"]
    assert report["checked_claim_count"] == 0
    assert report["mapping_repair_diagnostics"]["comparison_papers_without_records"] == ["A"]
    assert report["omitted"] == []
    assert calls == ["section-source-writing"]


@pytest.mark.parametrize("navigation", [
    "The present review focuses on the axial-chiral subset.",
    "The comparison proceeds by substrate family and tracks product class and substrate scope.",
    "本节按底物类别组织讨论，并比较不同方法的适用范围。",
])
def test_navigation_preserves_prose_without_mapping_model_and_still_gets_audited(navigation):
    text = source()["content"]
    paragraph = {"text": text + " " + navigation, "claims": [prose_claim(text, text)]}
    original = deepcopy(paragraph)
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [paragraph]}
        assert label == "section-used-claim-check"
        assert "discourse_span_requires_source_review" in prompt
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": "", "reason": ""}]}
    writing, _, report = write_from_sources(section_id="S01", task=task(), evidence=[source()],
        context="", call=model, audit_mode="selective")
    assert paragraph == original
    assert writing["claims"][0]["claim"] == original["text"]
    assert report["checked_claim_count"] == 1
    assert calls == ["section-source-writing", "section-used-claim-check"]



def test_resumed_audit_accepts_checked_rewrite_after_style_request_was_spent():
    saved = {}
    replacement = "After 60 minutes at 25 C, Catalyst A achieved 90% pollutant degradation."
    def save(state):
        saved.clear()
        saved.update(deepcopy(state))
    def interrupted(prompt, schema, label):
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [prose_claim(source()["content"], source()["content"])]}]}
        raise RuntimeError("provider timeout")
    args = dict(section_id="S01", task=task(), evidence=[source()], context="", save_state=save)
    with pytest.raises(RuntimeError, match="provider timeout"):
        write_from_sources(**args, call=interrupted)
    assert saved["repair_attempted"]
    def resumed(prompt, schema, label):
        assert label == "section-used-claim-check"
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "rewritten", "text": replacement, "reason": "Faithful rewording."}]}
    writing, _, report = write_from_sources(**args, call=resumed, resume_state=deepcopy(saved))
    assert writing["claims"][0]["claim"] == replacement
    assert report["omitted"] == []
