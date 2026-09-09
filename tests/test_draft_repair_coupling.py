"""Evaluation/repair boundary regressions, independent of review subject."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_core.draft_issue_routing import (
    paragraph_repair_contract, planning_adjustments, repair_input_fingerprint, route_draft_issue,
)
from review_writer_api.domain_services.actions.planning.blueprint import PlanningBlueprintActionsMixin
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_handlers.draft import DraftJobHandlers
from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT, DRAFT_QUALITY_REPORT, MATRIX


@pytest.fixture(scope="module")
def feedback():
    path = Path(__file__).resolve().parents[1] / "skills/review-first-draft-feedback-loop/scripts/feedback_loop.py"
    spec = importlib.util.spec_from_file_location("coupling_feedback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(*, core=False):
    return {"paper_ids": ["paper-a"], "original_source_ready": True,
            "argument_plan": [{"claim_id": "claim-a", "required_for_section": core,
                               "proposition": "The reported effect applies within the observed population."}],
            "evidence": [{"paper_id": "paper-a", "original_text_available": True,
                          "original_passages": [{"ref": "source-a", "text": "The population studied was limited."}]}]}


def finding(**overrides):
    return {"paragraph_id": "section-a-p1", "source_check_status": "verified",
            "route": "section_rewrite", "failed_dimensions": ["P02"], "severity": "major", **overrides}


def test_verified_attribution_defect_overrides_legacy_polish_and_evidence_route(feedback):
    row = finding(route="final_polish", failed_dimensions=["C03"], unsupported_claims=[],
                  evidence_problem_type="none", repair_stage="evidence_package",
                  repair_route="targeted_evidence_then_paragraph_rewrite",
                  diagnosis="The sentence does not identify whose prior results were unreproduced.")
    contract = paragraph_repair_contract(row, evidence())
    assert contract["repair_stage"] == "draft"
    assert contract["repair_class"] == "draft_rewrite"
    assert contract["evidence_rescue_eligible"] is False
    assert contract["required_inputs_ready"] is True
    mode = feedback.automatic_rewrite_mode(row, evidence(), paragraph_goal=85)
    assert mode == "source_recheck_cleanup"
    prompt = feedback.rewrite_prompt({"paragraph_id": "p1", "text": "Prior results were not reproduced."},
                                     row, evidence(), 1, 200, rewrite_mode=mode)
    assert "FACTUAL REPAIR takes precedence" in prompt
    assert "smallest sentence-level change" in prompt
    missing = {"paper_ids": ["paper-a"], "evidence": []}
    assert feedback.automatic_rewrite_mode(row, missing, paragraph_goal=85) == ""


def test_issue_identity_survives_repair_route_change():
    from review_writer_core.draft_issue_routing import issue_fingerprint
    from review_writer_api.domain_services.drafts import DraftsService
    row = finding(failed_dimensions=["C03"], paper_ids=["A"], claim_ids=["C1"])
    old = {**row, "repair_route": "targeted_evidence_then_paragraph_rewrite", "repair_class": "evidence_rescue_then_rewrite"}
    new = {**row, "repair_route": "paragraph_rewrite", "repair_class": "draft_rewrite"}
    assert issue_fingerprint(row, old) == issue_fingerprint(row, new)
    before, _ = DraftsService._quality_root_causes([old])
    after, _ = DraftsService._quality_root_causes([new])
    assert before[0]["root_cause_id"] == after[0]["root_cause_id"]
    assert DraftsService._repair_summary({"root_causes": before}, after)["resolved_root_cause_ids"] == []
    legacy = {**before[0], "root_cause_id": "ROOT-OLD-ROUTE-HASH"}
    assert DraftsService._repair_summary({"root_causes": [legacy]}, after)["resolved_root_cause_ids"] == []


def test_failed_automation_is_not_a_user_decision():
    from review_writer_api.domain_services.drafts import DraftsService
    roots, _ = DraftsService._quality_root_causes([{
        "paragraph_id": "p1", "repair_route": "paragraph_rewrite", "repair_class": "draft_rewrite",
        "auto_repairable": False, "evidence_rescue_eligible": False,
    }])
    assert roots[0]["requires_user_decision"] is False


def test_interactive_entry_cannot_bypass_core_argument_permissions(feedback):
    row = finding(source_check_status="partially_supported", unsupported_claims=["An unsupported core conclusion"],
                  route="section_rewrite", failed_dimensions=["C03"])
    assert paragraph_repair_contract(row, evidence(core=True))["rewrite_eligible"] is False
    assert feedback.automatic_rewrite_mode(row, evidence(core=True), paragraph_goal=85) == ""
    assert feedback.interactive_rewrite_mode(row, evidence(core=True), paragraph_goal=85) == ""


def test_stale_corrections_do_not_bypass_source_or_stage_requirements(feedback):
    row = finding(source_check_status="contradicted", failed_dimensions=["C02"],
                  source_corrections=[{"before": "old", "after": "new"}])
    assert feedback.automatic_rewrite_mode(row, {"evidence": []}, paragraph_goal=85) == ""
    supplied = evidence()
    assert feedback.automatic_rewrite_mode(row, supplied, paragraph_goal=85) == "source_correction"


def test_real_passages_do_not_require_a_duplicate_availability_flag(feedback):
    row = finding(source_check_status="unsupported", unsupported_claims=["A result not supported here"], failed_dimensions=["C02"])
    supplied = evidence()
    supplied["evidence"][0].pop("original_text_available")
    assert feedback.automatic_rewrite_mode(row, supplied, paragraph_goal=85) == "source_recheck_cleanup"


def test_better_score_and_polish_route_do_not_resolve_remaining_evidence_defect(feedback):
    original = "# Review\n\nThe study discussed results [1].\n\n<!-- paragraph_id: p1 -->\n"
    candidate = original.replace("discussed", "described")
    old = {"paragraph_id": "p1", "score": 86, "route": "section_rewrite",
           "source_check_status": "verified", "failed_dimensions": ["C03"], "unsupported_claims": []}
    new = {**old, "score": 95, "route": "final_polish"}
    best = {}
    excluded = feedback.update_best_paragraph_candidates(best,
        source_markdown=original, candidate_markdown=candidate,
        source_evaluation={"paragraph_scores": [old]}, candidate_evaluation={"paragraph_scores": [new]},
        source_preflight={}, candidate_preflight={}, candidate_evidence={}, min_words=1, max_words=200, iteration=1)
    assert best == {}
    assert "target_issue_not_resolved" in excluded[0]["reasons"]


def test_low_score_vague_wish_cannot_authorize_rewrite(feedback):
    row = finding(score=10, failed_dimensions=[], diagnosis="Make this much better.", auto_repairable=True)
    assert paragraph_repair_contract(row, evidence())["repair_class"] == "advisory"
    assert feedback.automatic_rewrite_mode(row, evidence(), paragraph_goal=85) == ""
    partial = {**row, "source_check_status": "partially_supported"}
    assert feedback.automatic_rewrite_mode(partial, evidence(), paragraph_goal=85) == ""
    # Explicitly requested optional candidates remain available to the user.
    assert paragraph_repair_contract(row, evidence())["interactive_rewrite_eligible"]


@pytest.mark.parametrize("core,expected", [(True, "planning_adjustment"), (False, "claim_narrowing")])
def test_completed_no_hit_respects_claim_role(feedback, core, expected):
    row = finding(source_check_status="not_found_in_checked_scope", evidence_rescue_status="not_found_in_checked_scope",
                  unsupported_claims=["The result applies to every population."])
    capability = paragraph_repair_contract(row, evidence(core=core))
    assert capability["repair_class"] == expected
    assert capability["rewrite_eligible"] is not core
    assert bool(feedback.automatic_rewrite_mode(row, evidence(core=core), paragraph_goal=85)) is not core


def test_unsupported_core_does_not_block_supported_omitted_core(feedback):
    row = finding(missing_core_claim_ids=["claim-a"])
    assert feedback.automatic_rewrite_mode(row, evidence(core=True), paragraph_goal=85) == "section_rewrite"


def test_model_no_hit_is_not_a_completed_local_investigation(feedback):
    row = finding(source_check_status="not_found_in_checked_scope",
                  unsupported_claims=["The result applies to every population."])
    for core in (True, False):
        capability = paragraph_repair_contract(row, evidence(core=core))
        assert capability["repair_class"] == "evidence_rescue_then_rewrite"
        assert capability["evidence_rescue_eligible"]
        assert not capability["rewrite_eligible"]


def test_recovery_projection_does_not_restore_severity_only_gate(feedback):
    row = finding(score=50, failed_dimensions=[], diagnosis="Optional improvement")
    result = feedback.apply_evidence_rescue_outcomes({"paragraph_scores": [row]}, {},
        paragraph_goal=85, evidence={row["paragraph_id"]: evidence()})
    assert not result["blocking_paragraph_failures"]
    assert result["decision"] == "PASS"


def test_missing_refs_does_not_prove_contradiction_or_force_rewrite(feedback):
    row = finding(source_check_status="needs_human_review", failed_dimensions=[])
    capability = paragraph_repair_contract(row, evidence())
    assert capability["evidence_rescue_eligible"]
    assert not capability["blocking"]
    assert feedback.automatic_rewrite_mode(row, evidence(), paragraph_goal=85) == ""


def test_conflict_cannot_be_overwritten_by_old_no_hit(feedback):
    row = finding(source_check_status="contradicted", unsupported_claims=["Effect direction differs from the source."])
    evaluated = feedback.apply_evidence_rescue_outcomes({"paragraph_scores": [row]},
        {"evidence_rescue_outcomes": {row["paragraph_id"]: {"status": "not_found_in_checked_scope"}}},
        paragraph_goal=85, evidence={row["paragraph_id"]: evidence()})
    assert evaluated["paragraph_scores"][0]["repair_class"] == "human_confirmation"
    assert evaluated["blocking_paragraph_failures"]


def test_reused_source_projection_keeps_core_role(feedback, tmp_path):
    (tmp_path / "04_first_draft").mkdir()
    (tmp_path / "04_first_draft/first_draft.md").write_text("Draft", encoding="utf-8")
    stored = feedback.original_source_check_report(tmp_path, {}, {"p1": evidence(core=True)})
    restored = feedback.evidence_from_source_check_report(stored)
    assert restored["p1"]["argument_plan"] == evidence(core=True)["argument_plan"]


def test_problem_identity_and_execution_input_are_separate():
    issue = finding(paper_ids=["paper-a"], claim_ids=["claim-a"])
    first = route_draft_issue({**issue, "diagnosis": "First wording"})
    second = route_draft_issue({**issue, "diagnosis": "Different wording", "source_revision": 2})
    assert first["issue_fingerprint"] == second["issue_fingerprint"]
    assert first["issue_fingerprint"] != route_draft_issue({**issue, "claim_ids": ["claim-b"]})["issue_fingerprint"]
    before = repair_input_fingerprint({"text": "Draft"}, issue, evidence(core=False), constraints={})
    after = repair_input_fingerprint({"text": "Draft"}, issue, evidence(core=True), constraints={})
    assert before != after




def test_grouped_adjustments_are_specific_and_do_not_mutate_source():
    issue = {**finding(), "repair_class": "planning_adjustment", "section_id": "s1",
             "claim_ids": ["c1"], "planning_claims": [{"claim_id": "c1", "original_text": "Original core assertion"}]}
    groups = planning_adjustments([issue, {**issue, "paragraph_id": "p2"}])
    assert len(groups) == 1
    assert groups[0]["claims"] == issue["planning_claims"]
    assert issue["planning_claims"][0]["original_text"] == "Original core assertion"


def test_historical_planning_candidate_still_checks_source_ids():
    service = PlanningBlueprintActionsMixin()
    service.repository = Mock()
    current = {DRAFT_QUALITY_REPORT: "q1", DRAFT_MANUSCRIPT: "d1", MATRIX: "m1"}
    service.repository.get_current_artifact.side_effect = lambda _u, _p, name: SimpleNamespace(id=current[name])
    # New Draft requests cannot create Blueprint repair jobs. Old in-flight
    # candidates must still retain their source-version guard.
    assert not hasattr(service, "attach_draft_repair_context")
    prepared = {"draft_repair_input_artifacts": {MATRIX: "m1"}}
    current[MATRIX] = "m2"
    with pytest.raises(WorkflowConflict):
        service.validate_prepared_blueprint(SimpleNamespace(user_id="user1"), "project1", prepared)
