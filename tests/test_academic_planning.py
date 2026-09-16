from copy import deepcopy
import json
from threading import Barrier, Lock, get_ident
from types import SimpleNamespace

import pytest

from review_writer_core.stages.planning.academic_planning import enhance_blueprint
from review_writer_core.claim_contracts import claim_is_executable, normalize_section_claim_contract


def test_missing_abstract_uses_bounded_source_passages_and_changes_cache():
    from review_writer_core.stages.planning.academic_planning import _structure_contributions, _fingerprint
    row = {"paper_id": "P1", "abstract": "abstract unavailable or unreliable",
           "planning_source_passages": [{"chunk_id": "procedure", "source_lineage_hash": "v1",
                                         "page_start": 1, "content": "A reproducible preparation. " * 300}]}
    context = _structure_contributions([row], 4000)
    assert context[0]["abstract"] == ""
    assert context[0]["source_passages"][0]["content"].startswith("A reproducible preparation.")
    assert len(context[0]["source_passages"][0]["content"]) <= 3600
    original = _fingerprint(context)
    row["planning_source_passages"][0]["source_lineage_hash"] = "v2"
    assert original != _fingerprint(_structure_contributions([row], 4000))


def test_structure_context_uses_verified_facts_without_making_them_mandatory():
    from review_writer_core.stages.planning.academic_planning import _structure_contributions

    rows = [
        {
            "paper_id": "P1",
            "abstract": "A study of one method.",
            "scientific_facts": [
                {
                    "fact_id": "F1",
                    "field_id": "quantitative_results",
                    "value": "The isolated yield was 91%.",
                    "support_level": "direct",
                    "assertion_ceiling": "direct_source_report",
                    "evidence_refs": [{"evidence_key": "k1"}],
                },
                {
                    "fact_id": "F2",
                    "field_id": "topic_partition",
                    "fact_type": "classification",
                    "value": "Substrate class A",
                    "support_level": "direct",
                    "evidence_refs": [{"evidence_key": "k2"}],
                },
            ],
        },
        {"paper_id": "P2", "abstract": "A second study."},
    ]
    context = _structure_contributions(rows, 4000)
    assert [fact["fact_id"] for fact in context[0]["verified_facts"]] == ["F1"]
    assert context[1]["verified_facts"] == []


def test_planning_only_matrix_context_is_preferred_without_changing_candidate():
    data = prepared()
    data["matrix_snapshot"] = {
        "rows": [{"paper_id": "P1", "abstract": "Persisted metadata only."}]
    }
    data["planning_matrix_snapshot"] = {
        "rows": [
            {
                "paper_id": "P1",
                "abstract": "",
                "planning_source_passages": [
                    {
                        "chunk_id": "source-1",
                        "source_lineage_hash": "v1",
                        "content": "Retrieval-only passage for planning.",
                    }
                ],
                "scientific_facts": [
                    {
                        "fact_id": "F-PLAN",
                        "field_id": "scope",
                        "value": "The source reports one bounded substrate class.",
                        "support_level": "direct",
                        "evidence_refs": [{"evidence_key": "source-1"}],
                    }
                ],
            }
        ]
    }

    def model(prompt, *, label):
        assert "Retrieval-only passage for planning." in prompt
        assert "F-PLAN" in prompt
        assert "Persisted metadata only." not in prompt
        if label == "blueprint-structure":
            return {
                "sections": [
                    {
                        **data["section_blueprint"]["sections"][0],
                        "review_problem": "What is established?",
                    }
                ],
                "unused_papers": [],
            }
        return proposal()

    result = enhance_blueprint(
        data, model_call=model, checkpoint={}, report=lambda *a: None
    )
    section = result["section_blueprint"]["sections"][0]
    assert section["evidence_readiness"]["status"] == "fact_assisted"
    assert section["planning_fact_context"]["fact_ids"] == ["F-PLAN"]
    assert data["matrix_snapshot"]["rows"][0] == {
        "paper_id": "P1",
        "abstract": "Persisted metadata only.",
    }


@pytest.mark.parametrize("reason,retained", [("insufficient_evidence", True), ("out_of_scope", False)])
def test_context_gap_keeps_existing_route_but_scope_exclusion_is_preserved(reason, retained):
    from review_writer_core.stages.planning.academic_planning import plan_structure
    data = prepared()
    data["matrix_snapshot"] = {"rows": [{"paper_id": "P1", "abstract": ""}]}
    def model(prompt, **kwargs):
        assert "Missing abstracts or fact cards" in prompt
        return {"sections": [{"section_id": "S01", "title": "One category", "section_role": "body",
                              "review_problem": "Which methods are demonstrated?", "primary_papers": []}],
                "unused_papers": [{"paper_id": "P1", "reason_code": reason, "reason": "Requires source review"}]}
    sections, unused = plan_structure(data, model, {})
    assert ("P1" in sections[0]["primary_papers"]) is retained
    assert bool(unused) is not retained


def prepared():
    return {"section_blueprint": {"review_topic": "Methods of synthesis", "sections": [
        {"section_id": "S01", "section_role": "body", "title": "One category", "primary_papers": ["P1"],
         "section_thesis": "Basic bounded thesis"},
    ]}, "facts_by_paper": {"P1": [{"fact_id": "F1", "field_id": "quantitative_results", "value": "91% yield", "support_level": "direct",
        "assertion_ceiling": "direct_source_report",
        "support_excerpt": "The experiment afforded 91% yield.", "evidence_refs": [{"evidence_key": "k"}]}]}}


def proposal():
    return {"question": "What is established for this transformation?", "purpose": "Assess demonstrated results.",
            "questions_to_answer": ["What results were reported?"], "retrieval_directions": ["Conditions and reported results"],
            "thesis": "The experiment afforded 91% yield.", "fact_ids": ["F1"], "comparison_axes": ["yield"],
            "boundaries": ["One experiment does not establish general performance."],
            "counterevidence_fact_ids": [], "open_questions": ["How broad is the demonstrated scope?"]}


def test_contribution_navigation_and_section_thread_are_preserved():
    from review_writer_core.stages.planning.academic_planning import _structure_contributions
    context = _structure_contributions([{"paper_id": "P1", "paper_analysis": {
        "research_question": "Which problem is solved?", "contribution": "A bounded result", "fact_ids": []}}], 4000)
    assert context[0]["paper_analysis"]["usage"] == "navigation_only"
    planned = {**proposal(), "organizing_thread": "Scope then comparison", "paragraph_tasks": ["Explain scope"],
               "paper_roles": [{"paper_id": "P1", "role": "main_progress", "presentation": "table", "reason": "Conditions are comparable"}]}
    result = enhance_blueprint(prepared(), model_call=lambda *a, **k: planned, checkpoint={}, report=lambda *a: None)
    section = result["section_blueprint"]["sections"][0]
    assert section["organizing_thread"] == planned["organizing_thread"]
    assert section["paragraph_tasks"] == ["Explain scope"]
    assert section["paper_roles"][0]["presentation"] == "table"


def test_malformed_paragraph_tasks_are_not_split_into_characters():
    result = enhance_blueprint(prepared(), model_call=lambda *a, **k: {**proposal(), "paragraph_tasks": "bad"},
                               checkpoint={}, report=lambda *a: None)
    section = result["section_blueprint"]["sections"][0]
    assert section["planning_status"] != "planned"



def test_one_provisional_call_and_reuse_without_audit_or_repair():
    data, checkpoint, calls = prepared(), {"replan_rounds": 1, "fact_repair_completed": True}, []
    original = deepcopy(data)
    def model(prompt, *, label):
        calls.append(label)
        return proposal()
    for _ in range(2):
        result = enhance_blueprint(data, model_call=model, checkpoint=checkpoint, report=lambda *a: None)
    section = result["section_blueprint"]["sections"][0]
    assert calls == ["blueprint-plan-S01"]
    assert section["thesis_status"] == "provisional"
    assert section["scientific_thesis"]["provisional"]
    assert section["generation_eligible"]
    assert section["evidence_readiness"]["status"] == "not_reviewed"
    assert not any(c.get("claim_id") == "S01-THESIS" for c in section["scientific_claims"])
    assert "replan_rounds" not in checkpoint and "fact_requests_by_paper" not in result
    assert data == original


def test_existing_fact_verification_is_preserved_without_verifying_the_new_thesis():
    from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION, review_fingerprint
    data = prepared()
    fact = data["facts_by_paper"]["P1"][0]
    fact["validation_contract"] = FACT_VALIDATION_VERSION
    fact["verification"] = {"contract": FACT_VALIDATION_VERSION, "status": "supported",
                            "input_fingerprint": review_fingerprint(fact)}
    result = enhance_blueprint(data, model_call=lambda *a, **k: {**proposal(), "thesis": "Explore a broader hypothesis"},
                               checkpoint={}, report=lambda *a: None)
    section = result["section_blueprint"]["sections"][0]
    claims = normalize_section_claim_contract(section)["scientific_claims"]
    assert claims == []
    assert data["facts_by_paper"]["P1"][0] == fact
    assert section["writing_objective"] == proposal()["purpose"]
    assert section["thesis_status"] == "provisional"


def test_global_structure_is_planned_once_before_section_requests_and_cached():
    data, checkpoint, calls = prepared(), {}, []
    data["matrix_snapshot"] = {"rows": [{"paper_id": "P1"}]}
    def model(prompt, *, label):
        calls.append(label)
        if label == "blueprint-structure":
            return {"sections": [{**data["section_blueprint"]["sections"][0], "review_problem": "What is established?"}], "unused_papers": []}
        return proposal()
    for _ in range(2):
        enhance_blueprint(data, model_call=model, checkpoint=checkpoint, report=lambda *a: None)
    assert calls == ["blueprint-structure", "blueprint-plan-S01"]


def test_interruption_before_structure_completion_does_not_erase_section_checkpoints():
    data, checkpoint = prepared(), {}
    enhance_blueprint(data, model_call=lambda *a, **k: proposal(), checkpoint=checkpoint, report=lambda *a: None)
    saved = deepcopy(checkpoint["sections"])
    def stopped(*args):
        assert args[2]["sections"] == saved
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        enhance_blueprint(data, model_call=lambda *a, **k: proposal(), checkpoint=checkpoint, report=stopped)
    assert checkpoint["sections"] == saved


@pytest.mark.parametrize("facts,patch", [
    (None, {"fact_ids": ["F999"]}), (None, {"thesis": "CuBr gave 96% yield."}),
    ([], {"fact_ids": []}),
])
def test_evidence_gaps_and_unverified_thesis_never_block_planning(facts, patch):
    data = prepared()
    if facts is not None:
        data["facts_by_paper"]["P1"] = facts
    result = enhance_blueprint(data, model_call=lambda *a, **k: {**proposal(), **patch}, checkpoint={}, report=lambda *a: None)
    section = result["section_blueprint"]["sections"][0]
    assert result["section_blueprint"]["academic_planning"]["status"] == "completed"
    assert section["generation_eligible"] and section["thesis_status"] == "provisional"
    assert "fact_ids" not in section["scientific_thesis"]
    assert all(c["required_for_section"] is False for c in section["scientific_claims"])
    assert all(c.get("proposition") != "CuBr gave 96% yield." for c in section["scientific_claims"])
    assert section["planning_notes"] == []


def test_concurrency_is_bounded_and_checkpointing_preserves_outline_order():
    data, checkpoint = prepared(), {}
    data["section_blueprint"]["sections"] = [
        {**data["section_blueprint"]["sections"][0], "section_id": f"S{i:02d}"} for i in range(1, 5)]
    barrier, lock = Barrier(2), Lock()
    running, peak, calls, reported = 0, 0, [], []
    owner = get_ident()
    def model(prompt, *, label):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            calls.append(label)
        barrier.wait(timeout=10)
        with lock:
            running -= 1
        return proposal()
    def report(current, total, state):
        assert get_ident() == owner
        json.dumps(state)
        reported.append(current)
    result = enhance_blueprint(data, model_call=model, checkpoint=checkpoint, report=report)
    assert peak == 2 and len(calls) == 4
    assert result["section_blueprint"]["academic_planning"]["status"] == "completed"
    assert [s["section_id"] for s in result["section_blueprint"]["sections"]] == ["S01", "S02", "S03", "S04"]
    assert reported == sorted(reported) and reported[-1] == 4


@pytest.mark.parametrize("failure", ["outage", "schema", "unknown_paper"])
def test_partial_failure_resumes_only_failed_section(failure):
    data, checkpoint, calls = prepared(), {}, []
    data["section_blueprint"]["sections"].append({**data["section_blueprint"]["sections"][0], "section_id": "S02"})
    def model(prompt, *, label):
        calls.append(label)
        if label.endswith("S02") and calls.count(label) == 1:
            if failure == "outage":
                raise RuntimeError("provider unavailable")
            if failure == "schema":
                return {**proposal(), "purpose": ""}
            return {**proposal(), "paper_roles": [{"paper_id": "P999", "role": "background"}]}
        return proposal()
    first = enhance_blueprint(data, model_call=model, checkpoint=checkpoint, report=lambda *a: None)
    assert first["section_blueprint"]["academic_planning"]["incomplete_sections"] == ["S02"]
    assert first["section_blueprint"]["academic_planning"]["resume_available"]
    second = enhance_blueprint(data, model_call=model, checkpoint=checkpoint, report=lambda *a: None)
    assert second["section_blueprint"]["academic_planning"]["status"] == "completed"
    assert calls.count("blueprint-plan-S01") == 1 and calls.count("blueprint-plan-S02") == 2


def test_optional_fact_edit_does_not_invalidate_planning_and_scope_invalidates_all():
    data, checkpoint, calls = prepared(), {}, []
    data["section_blueprint"]["sections"].append({"section_id": "S02", "section_role": "body", "title": "Other", "primary_papers": ["P2"]})
    data["facts_by_paper"]["P2"] = [{**data["facts_by_paper"]["P1"][0], "fact_id": "F2"}]
    def model(prompt, *, label):
        calls.append(label)
        return {**proposal(), "fact_ids": ["F2" if label.endswith("S02") else "F1"]}
    def run():
        return enhance_blueprint(data, model_call=model, checkpoint=checkpoint, report=lambda *a: None)
    run()
    data["facts_by_paper"]["P1"][0]["evidence_ceiling"] = "This experiment only."
    run()
    assert len(calls) == 2
    data["section_blueprint"]["scope_contract"] = {"target_question": "Which result transfers?"}
    run()
    assert len(calls) == 4


def test_worker_runs_once_caps_parallelism_and_publishes_progress(tmp_path):
    from review_writer_api.job_handlers.planning import PlanningJobHandlers
    data, runs, partials = prepared(), [], []
    data["blueprint_checkpoint"] = {"fact_repair_completed": True, "replan_rounds": 1}
    data["academic_planning_limits"] = {"section_concurrency": 3}
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
    def run(command, **kwargs):
        inputs = json.loads((tmp_path / "blueprint-input.json").read_text())
        assert inputs["academic_planning_limits"]["section_concurrency"] == 2
        state = json.loads((tmp_path / "blueprint-checkpoint.json").read_text())
        def report(current, total, saved):
            write(tmp_path / "blueprint-checkpoint.json", saved)
            write(tmp_path / "blueprint-progress.json", {"current": current, "total": total, **saved["progress"]})
            kwargs["progress_callback"]()
        output = enhance_blueprint(inputs, model_call=lambda *a, **k: proposal(), checkpoint=state, report=report)
        write(tmp_path / "blueprint-output.json", output)
        runs.append(command)
    handler = SimpleNamespace(root=tmp_path, runner=SimpleNamespace(run=run), _staging=lambda *a: tmp_path,
        model_gateway=SimpleNamespace(settings=SimpleNamespace(model_gateway_max_concurrency=4, model_gateway_user_concurrency=2)),
        _write_json=write, _result=lambda directory, name: json.loads((directory / name).read_text()),
        _text_gateway_environment=lambda context: ({}, {}))
    context = SimpleNamespace(user_id="owner", job_id="job", checkpoint=lambda: None, cancellation_requested=lambda: False,
        report_progress=lambda *a: None, report_partial_result=partials.append)
    result = PlanningJobHandlers.blueprint_plan(handler, context, data)
    assert len(runs) == 1 and not result["planning_resume_required"]
    assert partials[0]["blueprint_progress"]["phase"] == "structure"
    assert partials[-1]["blueprint_progress"]["phase"] == "completed"
    assert len(partials) == len({json.dumps(p, sort_keys=True) for p in partials})


def test_all_experiments_are_available_without_a_one_fact_per_field_template():
    from review_writer_core.claim_contracts import build_fact_grounded_claims
    fact = prepared()["facts_by_paper"]["P1"][0]
    fact["verification"] = {"status": "supported", "contract": "fact-support/3"}
    facts = [{**fact, "fact_id": fid, "experiment_id": experiment} for fid, experiment in [("F1", "main"), ("F2", "control")]]
    def project(items):
        return build_fact_grounded_claims(section_id="S01", section_title="Result", primary_papers=["P1"],
            required_fact_roles=[], rows_by_id={"P1": {"scientific_facts": items}})
    assert {c["claim_id"] for c in project(facts)} == {c["claim_id"] for c in project(list(reversed(facts)))}
    assert len(project(facts)) == 2


def test_reordering_manual_headings_keeps_question_ids():
    from review_writer_core.stages.planning.academic_planning import plan_structure
    data = prepared()
    data["section_blueprint"]["sections"][0]["review_problem"] = "What is the result?"
    data["section_blueprint"]["sections"].append({"section_id": "S02", "title": "Other category", "section_role": "body",
                                               "review_problem": "What is the boundary?", "primary_papers": ["P1"]})
    data["matrix_snapshot"] = {"rows": [{"paper_id": "P1", "scientific_facts": data["facts_by_paper"]["P1"]}]}
    data["outline_snapshot"] = {"manually_edited": True, "outline_md": "## One category\nPurpose: What is the result?\n## Other category\nPurpose: What is the boundary?"}
    checkpoint = {}
    model = lambda *a, **k: {"sections": deepcopy(data["section_blueprint"]["sections"]), "unused_papers": []}
    original, _ = plan_structure(data, model, checkpoint)
    data["section_blueprint"]["sections"].reverse()
    for index, section in enumerate(data["section_blueprint"]["sections"], 1):
        section["section_id"] = f"S{index:02d}"  # The outline parser numbers the new positions.
    data["outline_snapshot"]["outline_md"] = "## Other category\nPurpose: What is the boundary?\n## One category\nPurpose: What is the result?"
    reordered, _ = plan_structure(data, model, checkpoint)
    assert {s["title"]: s["section_id"] for s in original} == {s["title"]: s["section_id"] for s in reordered}
