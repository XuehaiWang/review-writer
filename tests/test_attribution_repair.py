from copy import deepcopy

from review_writer_core.source_attribution import validated_attribution_repair
from review_writer_core.draft_issue_routing import paragraph_repair_contract, select_rewrite_mode


TEXT = "The new sensor only works in dry air. Its response time is 12 ms."
OLD = "Our earlier sensor only worked in dry air because humidity affected its coating."
NEW = "Here we introduce a new coating and demonstrate operation in humid air."
EVIDENCE = {"paper_ids": ["P1"], "evidence": [{"paper_id": "P1", "original_passages": [
    {"ref": "p1:intro", "text": OLD + " " + NEW},
]}]}
PROPOSAL = {"kind": "wrong_study_attribution", "claim_span": "The new sensor only works in dry air.",
            "correction": "Attribute the dry-air restriction to the earlier sensor, not the new coating.",
            "context": [{"source_ref": "p1:intro", "quote": OLD},
                        {"source_ref": "p1:intro", "quote": NEW}]}


def finding(proposal=PROPOSAL):
    return {"paragraph_id": "S02-p4", "finding_category": "evidence",
            "source_check_status": "contradicted", "evidence_problem_type": "conflict",
            "unsupported_claims": [PROPOSAL["claim_span"]], "route": "human_confirmation",
            "source_evidence_refs": ["p1:intro"],
            "source_attribution_repair": validated_attribution_repair(TEXT, proposal, EVIDENCE)}


def test_attribution_conflict_uses_existing_rewrite_not_literal_replacement():
    issue = finding()
    contract = paragraph_repair_contract(issue, EVIDENCE)
    assert contract["rewrite_eligible"]
    assert contract["repair_action"] == "correct_source_grounded_prose"
    assert select_rewrite_mode(issue, EVIDENCE) == "source_recheck_cleanup"
    assert select_rewrite_mode(issue, EVIDENCE, interactive=True) == "source_recheck_cleanup"
    assert issue["source_check_status"] == "contradicted"
    assert not issue.get("source_corrections")  # no protected-signature bypass


def test_rejects_unregistered_or_fabricated_context():
    for field, value in [("source_ref", "unknown"), ("quote", "The experiment proves all sensors work everywhere.")]:
        proposal = deepcopy(PROPOSAL)
        proposal["context"][0][field] = value
        assert not validated_attribution_repair(TEXT, proposal, EVIDENCE)
        assert not paragraph_repair_contract(finding(proposal), EVIDENCE)["rewrite_eligible"]


def test_requires_current_unique_claim_and_both_contexts():
    assert not validated_attribution_repair("Different paragraph.", PROPOSAL, EVIDENCE)
    assert not validated_attribution_repair(TEXT + TEXT, PROPOSAL, EVIDENCE)
    proposal = deepcopy(PROPOSAL)
    proposal["context"] = [proposal["context"][0]] * 2
    assert not validated_attribution_repair(TEXT, proposal, EVIDENCE)


def test_unresolved_conflict_stays_manual_without_evidence():
    assert not paragraph_repair_contract(finding(None), EVIDENCE)["rewrite_eligible"]
    assert not paragraph_repair_contract(finding(), {"evidence": []})["rewrite_eligible"]


def test_api_preserves_source_guidance_for_candidate_buttons():
    from review_writer_api.domain_services.drafts import DraftsService
    score = finding()
    issues, _ = DraftsService._quality_routing({
        "issues": [{"issue_id": "I1", "paragraph_id": "S02-p4"}],
        "paragraph_scores": [score],
        "source_check": {"entries": [{"paragraph_id": "S02-p4", "papers": [
            {"paper_id": "P1", "passages": EVIDENCE["evidence"][0]["original_passages"]}]}]},
    }, {"section_index": {}, "section_evidence": {}})
    assert issues[0]["source_attribution_repair"] == score["source_attribution_repair"]
    assert issues[0]["rewrite_eligible"]
    assert issues[0]["repair_action"] == "correct_source_grounded_prose"
