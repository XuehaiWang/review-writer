"""Contribution-flow contracts, with no paid model requests or database writes."""
from copy import deepcopy
import pytest

from review_writer_core.source_attribution import section_navigation_context
from review_writer_core.stages.sections.coverage import section_input_fingerprints, matching_section_inputs
from review_writer_core.stages.sections.source_writing import write_from_sources, valid_source_claim
from review_writer_core.publication_tables import paper_presentation_outcomes
from review_writer_api.tests.test_feedback_loop_batching import feedback_loop as feedback


def test_section_input_change_keeps_independent_sections_but_refreshes_dependents():
    tasks = [{"section_id": sid, "section_role": role, "allowed_papers": [paper]}
             for sid, role, paper in [("S1", "body", "P1"), ("S2", "body", "P2"), ("S3", "body", "P1")]]
    tasks[2]["depends_on_sections"] = ["S1"]
    matrix = {"rows": [{"paper_id": "P1"}, {"paper_id": "P2"}]}
    blueprint = {"sections": deepcopy(tasks), "review_topic": "A general research topic"}
    shared = {"model": "test", "rules": "v1"}
    before = section_input_fingerprints(tasks, {}, matrix, blueprint, shared)
    entries = {sid: {"input_fingerprint": sig} for sid, sig in before.items()}
    tasks[0]["organizing_thread"] = "A different question progression"
    after = section_input_fingerprints(tasks, {}, matrix, blueprint, shared)
    retained, rejected = matching_section_inputs(entries, after, legacy_validated=True)
    assert set(retained) == {"S2"}
    assert set(rejected) == {"S1", "S3"}
    # Shared scientific scope and citation renumbering must invalidate all sections.
    blueprint["review_topic"] = "A new scope"
    changed = section_input_fingerprints(tasks, {}, matrix, blueprint, shared)
    assert all(after[sid] != changed[sid] for sid in after)
    matrix["rows"].reverse()
    renumbered = section_input_fingerprints(tasks, {}, matrix, blueprint, shared)
    assert all(changed[sid] != renumbered[sid] for sid in changed)


def test_navigation_uses_current_order_and_never_crosses_section_boundaries():
    plan = {"sections": [{"section_id": "S1", "organizing_thread": "Scope before comparison",
                          "paragraphs": [{"paragraph_id": "p1"}, {"paragraph_id": "p2"}]},
                         {"section_id": "S2", "paragraphs": [{"paragraph_id": "p3"}]}]}
    paragraphs = [{"paragraph_id": pid, "text": text} for pid, text in
                  [("p2", "Current edited second paragraph"), ("p1", "Current first paragraph"), ("p3", "Other section")]]
    contexts = section_navigation_context(plan, paragraphs)
    assert contexts["p1"]["previous"]["paragraph_id"] == "p2"
    assert "next" not in contexts["p1"]
    assert "previous" not in contexts["p3"]
    compact = feedback.compact_rewrite_evidence_for_prompt({"section_context": contexts["p1"]}, minimal=True)
    assert compact["section_context"]["organizing_thread"] == "Scope before comparison"
    assert compact["section_context"]["usage"] == "navigation_only_not_source_evidence"
    assert not compact["original_source_ready"]
    prompt = feedback.rewrite_prompt(paragraphs[1], {}, {"section_context": contexts["p1"]}, 10, 100)
    assert "Current edited second paragraph" in prompt
    assert "never copy a neighbor" in prompt
    from review_writer_core.draft_issue_routing import repair_input_fingerprint
    before = repair_input_fingerprint(paragraphs[1], {}, {"section_context": contexts["p1"]}, constraints={})
    contexts["p1"]["previous"]["text"] = "A corrected neighboring conclusion"
    after = repair_input_fingerprint(paragraphs[1], {}, {"section_context": contexts["p1"]}, constraints={})
    assert before != after


@pytest.mark.parametrize("order,valid", [(["S1-p2", "S1-p1"], True),
    (["S1-p2", "S1-p2"], False), (["S1-p2", "other-section"], False), ("S1-p2", False), (None, False)])
def test_auditor_reorders_once_without_renumbering_or_losing_claim_bindings(order, valid):
    source = {"evidence_key": "a", "paper_id": "P1", "content": "A method was demonstrated. Scope was examined."}
    calls = []
    def model(prompt, schema, label):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"claims": [{"text": text, "claim_kind": "reported_finding",
                "support_spans": [{"evidence_key": "E001", "quote": text}]}]}
                for text in ["A method was demonstrated.", "Scope was examined."]]}
        return {"claims": [{"claim_id": f"S1-p{i}-C01", "status": "supported", "text": text}
                for i, text in enumerate(["A method was demonstrated.", "Scope was examined."], 1)],
                "section_review": {"status": "coherent", "issues": [], "paragraph_order": order}}
    task = {"allowed_papers": ["P1"], "organizing_thread": "Scope then method"}
    writing, generated, report = write_from_sources(section_id="S1", task=task, evidence=[source], context="", call=model)
    assert len(calls) == 2
    expected = ["S1-p2", "S1-p1"] if valid else ["S1-p1", "S1-p2"]
    assert [p["paragraph_id"] for p in generated["paragraphs"]] == expected
    assert generated["paragraphs"][0]["claim_realizations"][0]["claim_id"] == expected[0] + "-C01"
    assert all(valid_source_claim(c, {"a": source}) for c in writing["claims"])
    assert writing["organizing_thread"] == task["organizing_thread"]
    assert report["section_review"]["status"] == ("coherent" if valid else "needs_revision")


def test_table_readiness_requires_actual_current_audited_export_rows():
    from review_writer_core.stages.sections.source_writing import support_fingerprint, CONTRACT
    paragraphs = []
    for paper, name in [("P1", "Material Alpha"), ("P2", "Material Beta")]:
        text = f"{name} achieved 90% conversion."
        refs = [{"evidence_key": paper, "quote": text, "paper_id": paper}]
        records = [{"evidence_key": paper, "object": name, "result": "90% conversion", "conditions": "25 C", "units": ""}]
        claim = {"claim_id": paper, "claim_kind": "reported_finding", "text": text,
                 "citation_group": [paper], "evidence_refs": refs, "result_context": records,
                 "source_verification": {"contract": CONTRACT, "status": "supported",
                    "input_fingerprint": support_fingerprint(text, refs, "reported_finding", records)}}
        paragraphs.append({"cited_paper_ids": [paper], "claim_realizations": [claim]})
    task = {"paper_roles": [{"paper_id": paper, "presentation": "table"} for paper in ["P1", "P2"]]}
    section = {"section_id": "S1", "section_role": "body", "primary_papers": ["P1", "P2"], "paragraphs": paragraphs}
    result = paper_presentation_outcomes(task, section, {}, {"P1": 1, "P2": 2})
    assert all(p["actual"] == "table_ready" for p in result)
    paragraphs[0]["claim_realizations"][0]["result_context"][0]["result"] = "99% conversion"
    result = paper_presentation_outcomes(task, section, {}, {"P1": 1, "P2": 2})
    assert all(p["actual"] != "table_ready" for p in result)


def test_planned_table_does_not_count_as_table_coverage_without_exportable_rows():
    task = {"paper_roles": [{"paper_id": "P1", "presentation": "table", "reason": "Use comparable conditions"},
                            {"paper_id": "P2", "presentation": "supporting_citation"}]}
    before = deepcopy(task)
    output = {"section_id": "S1", "paragraphs": [{"text": "A bounded finding [1].", "cited_paper_ids": ["P1"]}]}
    result = paper_presentation_outcomes(task, output, {}, {"P1": 1, "P2": 2})
    assert result[0]["actual"] == "cited_in_prose"
    assert result[0]["table_fallback"]
    assert result[1]["actual"] == "unrepresented"
    assert task == before


def test_shared_chapter_responsibility_change_invalidates_all_peer_prompts():
    tasks = [{"section_id": "A", "writing_objective": "Compare methods"},
             {"section_id": "B", "writing_objective": "Discuss scope"}]
    before = section_input_fingerprints(tasks, {}, {"rows": []}, {}, {})
    tasks[0]["avoid_points"] = ["Leave applicability limits to B"]
    after = section_input_fingerprints(tasks, {}, {"rows": []}, {}, {})
    assert all(before[sid] != after[sid] for sid in before)
