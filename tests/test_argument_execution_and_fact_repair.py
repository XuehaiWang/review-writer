from copy import deepcopy
from types import SimpleNamespace

import pytest

from review_writer_core.draft_issue_routing import draft_fact_repair_checkpoint, draft_fact_repair_requests
from review_writer_core.scientific_facts import (
    FACT_VALIDATION_VERSION, attach_repair_fact_context, is_additive_fact_repair,
)
from review_writer_core.section_narrative_contracts import build_argument_execution


def bundle(sentence="The external cohort accuracy was 84%."):
    source = {"evidence_key": "owned", "paper_id": "P1", "chunk_id": "chunk",
              "source_lineage_hash": "version1", "content": sentence, "content_type": "body"}
    fact = {"fact_id": "F1", "paper_id": "P1", "field_id": "quantitative_results", "value": sentence,
            "support_excerpt": sentence, "evidence_refs": [{"evidence_key": "owned", "support_excerpt": sentence}],
            "support_level": "direct", "validation_contract": FACT_VALIDATION_VERSION,
            "verification": {"contract": FACT_VALIDATION_VERSION, "status": "supported", "reason": "Matching experiment."}}
    matrix = {"rows": [{"paper_id": "P1", "scientific_facts": [fact]}]}
    blueprint = {"sections": [{"section_id": "S1", "title": "External validation", "section_role": "body",
                              "scientific_thesis": {"text": "Provisional unsupported superiority"}}]}
    claim = {"claim_id": "C1", "claim": sentence, "fact_ids": ["F1"], "citation_group": ["P1"],
             "evidence_refs": [{"evidence_key": "owned"}], "claim_kind": "reported_finding"}
    writing = {"sections": [{"section_id": "S1", "claims": [claim],
                              "paragraphs": [{"paragraph_id": "S1-p1", "claim_ids": ["C1"]}]}]}
    section_index = {"sections": [{"section_id": "S1", "paragraphs": [{"paragraph_id": "S1-p1", "text": sentence,
                      "claim_realizations": [{**claim, "text": sentence}]}]}]}
    return source, fact, matrix, blueprint, writing, section_index










@pytest.mark.parametrize("sentence", ["The external cohort accuracy was 84%.",
                                     "The alloy retained strength after cycling.",
                                     "The catalyst produced the isolated product."])
def test_execution_consumes_realized_prose_not_planned_thesis(sentence):
    _, _, matrix, blueprint, writing, section_index = bundle(sentence)
    execution = build_argument_execution(blueprint, writing, section_index, matrix,
                                         draft_text=f"## A\n\n{sentence}\n<!-- paragraph_id: S1-p1 -->")
    assert execution["sections"][0]["claims"][0]["claim"] == sentence
    assert not execution["semantic_quality_verified"]
    assert not execution["findings"]
    assert "Provisional" not in str(execution["sections"][0]["claims"])


def test_manual_edits_and_superseded_facts_do_not_inherit_old_bindings():
    _, fact, matrix, blueprint, writing, section_index = bundle()
    execution = build_argument_execution(blueprint, writing, section_index, matrix,
        draft_text="## A\n\nMy edited paragraph.\n<!-- paragraph_id: S1-p1 -->")
    assert not execution["sections"][0]["claims"]
    assert execution["findings"][0]["code"] == "claim_binding_not_current"
    fact["superseded_by_fact_id"] = "F2"
    execution = build_argument_execution(blueprint, writing, section_index, matrix)
    assert not execution["sections"][0]["claims"]


def test_cross_section_reuse_only_flags_identical_analysis():
    _, _, matrix, blueprint, writing, section_index = bundle()
    blueprint["sections"].append({"section_id": "S2", "title": "Limitations"})
    section_index["sections"].append(deepcopy(section_index["sections"][0]))
    other = section_index["sections"][-1]
    other["section_id"] = "S2"
    other["paragraphs"][0]["paragraph_id"] = "S2-p1"
    assert any(f["code"] == "repeated_claim_across_sections" for f in
               build_argument_execution(blueprint, writing, section_index, matrix)["findings"])
    other["paragraphs"][0]["text"] = "External validation is limited to one cohort."
    other["paragraphs"][0]["claim_realizations"][0]["text"] = other["paragraphs"][0]["text"]
    assert not any(f["code"] == "repeated_claim_across_sections" for f in
                   build_argument_execution(blueprint, writing, section_index, matrix)["findings"])


def test_unrelated_timestamp_does_not_change_execution_fingerprint():
    _, _, matrix, blueprint, writing, section_index = bundle()
    before = build_argument_execution(blueprint, writing, section_index, matrix)
    matrix["updated_at"] = "later"
    section_index["generated_at"] = "later"
    assert before["input_fingerprint"] == build_argument_execution(blueprint, writing, section_index, matrix)["input_fingerprint"]


def test_source_repair_groups_same_question_but_preserves_all_consumers():
    _, _, matrix, _, writing, _ = bundle()
    writing["sections"][0]["paragraphs"].append({"paragraph_id": "S1-p2", "claim_ids": ["C1"]})
    payload = {"matrix": matrix, "writing_plan": writing, "issues": [
        {"paragraph_id": pid, "repair_stage": "evidence_package", "auto_repairable": True}
        for pid in ("S1-p1", "S1-p2")]}
    roots = draft_fact_repair_requests(payload)
    assert len(roots) == 1
    assert roots[0]["paragraph_ids"] == ["S1-p1", "S1-p2"]
    payload["paragraph_id"] = "S1-p2"
    assert draft_fact_repair_requests(payload)[0]["paragraph_ids"] == ["S1-p2"]
    payload["issues"][1]["auto_repairable"] = False
    assert not draft_fact_repair_requests(payload)


def test_manual_source_conflict_gets_one_bounded_local_evidence_rescue():
    payload = {
        "matrix": {"rows": [{"paper_id": "P1"}, {"paper_id": "P2"}]},
        "writing_plan": {
            "sections": [
                {
                    "section_id": "S1",
                    "claims": [],
                    "paragraphs": [
                        {"paragraph_id": "S1-p1", "paper_ids": ["P1"]}
                    ],
                }
            ]
        },
        "issues": [
            {
                "paragraph_id": "S1-p1",
                "section_id": "S1",
                "repair_stage": "evidence_package",
                "auto_repairable": False,
                "evidence_rescue_eligible": True,
                "paper_ids": ["P1", "OUTSIDE_PROJECT"],
                "unsupported_claims": ["The reported selectivity is 98%."],
            }
        ],
    }

    requests = draft_fact_repair_requests(payload)

    assert len(requests) == 1
    assert requests[0]["paper_id"] == "P1"
    assert requests[0]["paragraph_ids"] == ["S1-p1"]
    assert requests[0]["evidence_rescue"]
    assert requests[0]["request"]["query"] == "The reported selectivity is 98%."




def repair_bundle():
    source, fact, matrix, _, _, _ = bundle()
    package = {"sections": [{"section_id": "S1", "primary_papers": ["P1"], "hits": []}], "evidence_registry": []}
    repair = {"targets": [{"paper_id": "P1", "paragraph_ids": ["S1-p1"]}],
              "papers": [{"paper_id": "P1", "facts": [fact]}],
              "fact_repair_sources": [{"paper_id": "P1", "evidence_candidates": [source]}]}
    return package, repair, matrix


def test_supplements_are_isolated_until_exact_sources_are_accepted():
    package, repair, matrix = repair_bundle()
    snapshot = deepcopy(package)
    candidate, _ = attach_repair_fact_context(package, repair)
    assert package == snapshot
    assert candidate["paragraph_fact_supplements"]["S1-p1"]
    declined, summary = attach_repair_fact_context(package, repair, selected_by_paragraph={})
    assert not declined["evidence_registry"] and not summary["promoted_facts"]
    accepted, summary = attach_repair_fact_context(package, repair, selected_by_paragraph={"S1-p1": ["owned"]})
    assert accepted["evidence_registry"]
    old = {"rows": [{"paper_id": "P1", "scientific_facts": []}]}
    new = deepcopy(old)
    new["rows"][0]["scientific_facts"] = [summary["promoted_facts"][0]["fact"]]
    new["fact_repair_history"] = [{"operation": "draft_targeted_fact_promotion"}]
    assert is_additive_fact_repair(old, new)


def test_repair_does_not_attach_every_preexisting_fact_to_each_paragraph():
    package, repair, _ = repair_bundle()
    repair["initial_fact_ids"] = {"P1": ["F1"]}
    candidate, summary = attach_repair_fact_context(package, repair)
    assert not summary["promoted_facts"]
    assert not candidate["evidence_registry"]


def test_single_candidate_evidence_publication_reuses_exact_scored_sources():
    from review_writer_api.domain_services.drafts import DraftsService

    package, repair, matrix = repair_bundle()
    _, _, _, _, writing, index = bundle()
    payload = {"section_evidence": package, "matrix": matrix, "writing_plan": writing, "section_index": index}
    before = deepcopy(payload)
    evaluation = {"paragraph_id": "S1-p1", "fact_agent_repair": repair,
                  "paragraph_score": {"paragraph_id": "S1-p1", "score": 90, "source_evidence_refs": ["owned"]},
                  "source_check_entry": {"paragraph_id": "S1-p1", "source_evidence_refs": ["owned"]}}
    candidate, summary, _ = DraftsService._single_paragraph_evidence_repair(payload, evaluation)
    assert payload == before
    assert candidate["paragraph_fact_supplements"]["S1-p1"]
    assert summary["promoted_facts"][0]["fact"]["fact_id"] == "F1"
    assert summary["added_evidence_count"] == 0
    assert summary["affected_paragraph_ids"] == ["S1-p1"]
    assert candidate["sections"][0]["hit_count"] == 1
    assert candidate["sections"][0]["claim_eligible_hit_count"] == 1
    evaluation["paragraph_score"]["source_evidence_refs"] = ["different"]
    _, summary, _ = DraftsService._single_paragraph_evidence_repair(payload, evaluation)
    assert not summary["promoted_facts"]








def test_fact_checkpoint_survives_unrelated_prose_edits_and_declined_candidates():
    checkpoint = {"entries": {"P1": {"source_fingerprint": "source-and-question", "agent_state": {"no_progress_requests": ["root"]}}}}
    payload = {"quality": {"current": False, "feedback_status": {"fact_repair_checkpoint": checkpoint}},
               "rewrite_candidates": [{"status": "rejected", "created_at": "later", "candidate_evaluation": {
                   "fact_agent_repair": {"matrix_enrichment_checkpoint": {"entries": {"P2": {"source_fingerprint": "other"}}}}}}]}
    recovered = draft_fact_repair_checkpoint(payload)
    assert recovered["entries"]["P1"] == checkpoint["entries"]["P1"]
    assert "P2" in recovered["entries"]


@pytest.mark.parametrize("edited", [False, True])
def test_final_consumers_use_current_prose_bindings(tmp_path, monkeypatch, edited):
    import importlib.util
    import json
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    root = Path(__file__).resolve().parents[1]

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, root / path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    conclusion = load("conclusion_argument_test", "skills/review-conclusion-generator/scripts/generate_conclusion1.py")
    overview = load("overview_argument_test", "skills/review-figure-style-redraw/scripts/generate_overview_figure.py")
    _, _, matrix, blueprint, writing, index = bundle()
    blueprint.update(review_topic="External validation of predictive models", taxonomy_profile="general_academic")
    project = tmp_path / "review-projects" / "example"
    for directory in ("01_matrix_outline", "02_section_drafting", "04_first_draft"):
        (project / directory).mkdir(parents=True)
    for path, data in (("01_matrix_outline/literature_matrix.json", matrix),
                       ("01_matrix_outline/section_blueprint.json", blueprint),
                       ("02_section_drafting/writing_plan.json", writing),
                       ("02_section_drafting/section_drafts.json", index)):
        (project / path).write_text(json.dumps(data), encoding="utf-8")
    sentence = "The external cohort accuracy was 84%."
    prose = "The revised discussion covers a different limitation." if edited else sentence
    draft = f"# External Validation\n\n## External validation\n\n{prose}\n<!-- paragraph_id: S1-p1 -->\n"
    (project / "04_first_draft/first_draft.md").write_text(draft, encoding="utf-8")
    # Exercise the real context-building entry point with the manual-output
    # path; no credentials or provider calls are used in this regression.
    monkeypatch.setattr(conclusion, "_load_dotenv_if_present", lambda *_: None)
    monkeypatch.setattr(conclusion, "resolve_api_key", lambda *_: "")
    monkeypatch.setattr(conclusion, "gateway_configured", lambda: False)
    args = SimpleNamespace(review_root=str(tmp_path), project_id="example", mode="orchestrated",
                           api_key="", base_url="", model="")
    assert conclusion.run(args) == 0
    context = json.loads((project / "04_first_draft/conclusion_context.json").read_text(encoding="utf-8"))
    assert len(context["claims"]) == (0 if edited else 1)
    assert prose in context["draft_text"]
    prompt = (project / "04_first_draft/conclusion_prompt.txt").read_text(encoding="utf-8")
    assert "Provisional unsupported superiority" not in prompt
    features = overview.extract_review_features(project)
    assert len(features["overview_content_contract"]["realized_claim_ids"]) == (0 if edited else 1)
    # Display text now comes from the concise content pack, not raw claims.
    assert sentence not in overview._build_metal_rows_text(features)
    features["_content_pack"] = {"module_summaries": overview._validated_module_summaries(
        {"module_summaries": [{"section_id": "S1", "summary": sentence, "claim_ids": ["C1"]}]},
        features,
    )}
    rows_text = overview._build_metal_rows_text(features)
    assert (sentence in rows_text) is not edited
    if edited:
        assert "omit unsupported" in rows_text


@pytest.mark.parametrize("damage", ["foreign_paper", "missing_span", "uncertain", "correction"])
def test_invalid_or_corrective_fact_never_silently_enters_current_draft(damage):
    package, repair, _ = repair_bundle()
    fact = repair["papers"][0]["facts"][0]
    if damage == "foreign_paper":
        repair["papers"][0]["paper_id"] = "P-other"
    elif damage == "missing_span":
        fact["evidence_refs"][0]["evidence_key"] = "forged"
    elif damage == "uncertain":
        fact["verification"]["status"] = "uncertain"
    else:
        fact["correction_of_fact_id"] = "Old"
    candidate, summary = attach_repair_fact_context(package, repair)
    assert not summary["promoted_facts"]
    assert not candidate["evidence_registry"]
