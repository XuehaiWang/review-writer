from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from review_writer_core.stages.sections.source_writing import (
    CONTRACT, write_from_sources, valid_source_claim,
)
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


def test_audited_rewrite_preserves_source_binding_without_an_extra_call():
    replacement = "After 60 minutes at 25 C, Catalyst A achieved 90% pollutant degradation."
    writing, generated, report, calls = write(audit_status="rewritten", replacement=replacement)
    assert writing["claims"][0]["claim"] == replacement
    assert writing["claims"][0]["evidence_refs"][0]["quote"] == source()["content"]
    assert valid_source_claim(writing["claims"][0], {"a": source()})
    assert len(calls) == 2
    assert report["omitted"] == []


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
    from review_writer_core.stages.sections.source_writing import PARAGRAPH_GUIDANCE
    _, _, _, calls = write()
    assert PARAGRAPH_GUIDANCE in calls[0][1]
    assert '"primary_papers": ["A"]' in calls[0][1]
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
        assert len(calls) == 1


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
def test_bad_source_selection_omits_claim_without_fact_repair(span):
    writing, _, report, calls = write(span=span)
    assert not writing["claims"] and report["omitted"]
    assert len(calls) == 1


def test_unsupported_ranking_is_narrowed_once_without_extracting_all_paper_facts():
    evidence = [source(), source("b", "B", "Catalyst B degraded 95% at 40 C after 30 minutes.")]
    safe = "The studies used different temperatures and reaction times."
    spans = [{"evidence_key": r["evidence_key"], "quote": r["content"]} for r in evidence]
    writing, _, report, calls = write(evidence, text="Catalyst B is superior to A.", audit_status="narrowed", replacement=safe, span=spans)
    assert writing["claims"][0]["claim"] == safe
    assert report["narrowed"] and not report["omitted"] and len(calls) == 2


def test_audit_cannot_introduce_unquoted_numbers_or_new_sources():
    writing, _, report, _ = write(audit_status="narrowed", replacement="The yield was 99%.")
    assert not writing["claims"] and report["omitted"]


def test_result_records_are_only_kept_for_actual_supported_claims():
    records = [{"evidence_key": "a", "object": "Catalyst A", "conditions": "25 C; 60 minutes", "result": "90%", "units": "%"}]
    writing, _, report, calls = write(records=records)
    assert writing["claims"][0]["result_context"] == records
    assert "Check result_context" in calls[1][1]
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


def test_real_generator_without_fact_cards_finishes_and_reuses_checkpoint(tmp_path, monkeypatch):
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
    monkeypatch.setattr(pipeline.sys, "argv", [str(SCRIPT), "--review-root", str(tmp_path), "--project-id", "demo", "--api-key", "test", "--model", "test"])
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *a: {})
    monkeypatch.setattr(pipeline, "load_blueprint_rule_pack", lambda *a: "Use evidence")
    monkeypatch.setattr(pipeline, "load_cross_study_synthesis_skill", lambda *a: "Use evidence")
    calls = []
    def model(prompt, schema, *args, label, **kwargs):
        calls.append(label)
        if label == "section-source-writing":
            return {"paragraphs": [{"role": "anchor_case", "claims": [{"text": evidence[0]["content"], "support_spans": [{"evidence_key": "a", "quote": evidence[0]["content"]}]}]}]}
        return {"claims": [{"claim_id": "S01-p1-C01", "status": "supported", "text": evidence[0]["content"]}]}
    monkeypatch.setattr(pipeline, "call_structured_llm", model)
    assert pipeline.main() == 0
    checkpoint = json.loads((stage / "section_checkpoints.json").read_text(encoding="utf-8"))
    assert checkpoint["entries"]["S01"]["output"]["paragraphs"]
    assert checkpoint["entries"]["S01"]["writing"]["evidence_mode"] == CONTRACT
    assert pipeline.main() == 0
    assert len(calls) == 2
