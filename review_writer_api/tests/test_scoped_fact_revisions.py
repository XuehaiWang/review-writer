"""Offline scope and exact-revision contracts shared by reading and verification."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.domain_services.actions.planning.matrix import PlanningMatrixActionsMixin
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION
from review_writer_core.stages.planning.fact_revision import revise_fact, review_fingerprint


def source_fact(paper_id, fact_id="original"):
    quote = "The protocol gave 91% yield and 96% selectivity."
    ref = {"evidence_key": "source-" + paper_id, "paper_id": paper_id,
           "source_file_id": paper_id, "chunk_id": "results", "source_lineage_hash": "v1",
           "support_excerpt": quote}
    return {"fact_id": fact_id, "paper_id": paper_id, "field_id": "quantitative_results",
            "value": "The protocol gave 91% yield.", "evidence_refs": [ref],
            "support_spans": [ref], "support_excerpt": quote,
            "evidence_ceiling": "Only this experiment.", "support_level": "direct",
            "assertion_ceiling": "direct_source_report", "validation_contract": FACT_VALIDATION_VERSION,
            "verification": {"status": "supported", "contract": FACT_VALIDATION_VERSION}}


def revised_row(paper_id):
    revised = revise_fact(source_fact(paper_id), {"value": "The product was obtained in 91% yield."})
    return {"paper_id": paper_id, "title": "A study", "scientific_facts": [revised,
            source_fact(paper_id, "untouched")],
            "paper_analysis": {"contribution": "An existing reading guide."},
            "fact_enrichment": {"status": "pending", "required_fact_roles": ["quantitative_results"]}}


@pytest.fixture
def revision_service(monkeypatch):
    service = PlanningMatrixActionsMixin()
    matrix = {"rows": [revised_row("P1"), revised_row("P2"),
                       {"paper_id": "P3", "scientific_facts": []}]}
    service._owned_project = Mock(return_value=SimpleNamespace(model_tier="sol"))
    service._matrix = Mock(return_value=(matrix, SimpleNamespace(id="matrix-v1")))
    service._catalog = Mock(return_value={})
    service._supporting_source_parents = Mock(return_value={})
    service.library_index = SimpleNamespace(enabled=True, summaries=Mock(side_effect=lambda _principal, ids: {
        pid: {"fulltext": "ready", "source_lineage_hash": "v1"} for pid in ids}))
    service.fact_source_candidates = Mock(side_effect=lambda _principal, row, facts, _lineages: {
        "source-" + row["paper_id"]: {"evidence_key": "source-" + row["paper_id"],
            "paper_id": row["paper_id"], "source_file_id": row["paper_id"], "chunk_id": "results",
            "source_lineage_hash": "v1", "content": facts[0]["support_excerpt"], "content_type": "text"}})
    service.repository = SimpleNamespace(session_factory=None, get_stage_state=Mock(return_value=SimpleNamespace(revision=2)))
    principal = SimpleNamespace(user_id="owner", require=Mock())
    monkeypatch.setattr("review_writer_api.domain_services.actions.planning.matrix.resolve_model_tier",
                        lambda *args: SimpleNamespace(model="offline-model"))
    return service, matrix, principal


def test_revision_payload_limits_sources_and_targets_to_selected_owned_papers(revision_service):
    service, matrix, principal = revision_service
    before = deepcopy(matrix)
    payload = service.fact_revision_payload(principal, "project", selected_paper_ids=["P2", "P2", "P3"])
    assert [p["paper_id"] for p in payload["papers"]] == ["P2"]
    assert len(payload["papers"][0]["revision_facts"]) == 1
    assert payload["pending_paper_count"] == 1
    service.library_index.summaries.assert_called_once_with(principal, ["P2"])
    assert matrix == before


@pytest.mark.parametrize("selected", [[], ["P3"]])
def test_empty_revision_selection_does_not_expand_or_require_unused_index(revision_service, selected):
    service, _, principal = revision_service
    service.library_index = None
    result = service.fact_revision_payload(principal, "project", selected_paper_ids=selected)
    assert result["papers"] == [] and result["pending_paper_count"] == 0
    service.fact_source_candidates.assert_not_called()


def test_revision_scope_rejects_unknown_paper_before_source_work(revision_service):
    service, _, principal = revision_service
    with pytest.raises(WorkflowValidationError, match="outside the current Matrix"):
        service.fact_revision_payload(principal, "project", selected_paper_ids=["P1", "foreign"])
    service.library_index.summaries.assert_not_called()
    service.fact_source_candidates.assert_not_called()


def test_unscoped_revision_payload_keeps_all_pending_revisions(revision_service):
    service, _, principal = revision_service
    result = service.fact_revision_payload(principal, "project")
    assert [p["paper_id"] for p in result["papers"]] == ["P1", "P2"]


def successful_output(paper):
    fact = deepcopy(paper["revision_facts"][0])
    fact.update(verification={"status": "supported", "contract": FACT_VALIDATION_VERSION,
                              "input_fingerprint": review_fingerprint(fact)},
                support_level="direct", assertion_ceiling="direct_source_report")
    return {"paper_id": paper["paper_id"], "facts": [fact]}


def test_row_merger_applies_verdict_but_preserves_user_text_other_facts_and_guide(revision_service):
    service, matrix, principal = revision_service
    paper = service.fact_revision_payload(principal, "project", selected_paper_ids=["P1"])["papers"][0]
    # The same exact revision contract can live inside an ordinary reading batch.
    paper.update(verify_only=True, reading_strategy="")
    row = deepcopy(matrix["rows"][0])
    before = deepcopy(row)
    output = successful_output(paper)
    output["facts"][0]["human_checked"] = True
    output["paper_analysis"] = {"contribution": "Do not replace the guide."}
    service.apply_fact_revision_result(row, paper, output)
    assert row["scientific_facts"][0]["value"] == before["scientific_facts"][0]["value"]
    assert row["scientific_facts"][0]["human_checked"] is False
    assert row["scientific_facts"][0]["verification"]["status"] == "supported"
    assert row["scientific_facts"][0]["review_status"] == "auto_resolved"
    assert row["scientific_facts"][1] == before["scientific_facts"][1]
    assert row["paper_analysis"] == before["paper_analysis"]
    assert row["fact_enrichment"]["pending_revision_fact_ids"] == []
    assert matrix["rows"][0] == before


@pytest.mark.parametrize("problem", ["paper", "missing_fact", "extra_fact", "duplicate_fact",
                                      "changed_value", "stale_verdict", "missing_source", "current_changed"])
def test_row_merger_rejects_wrong_or_stale_result_without_mutation(revision_service, problem):
    service, matrix, principal = revision_service
    paper = service.fact_revision_payload(principal, "project", selected_paper_ids=["P1"])["papers"][0]
    row = deepcopy(matrix["rows"][0])
    output = successful_output(paper)
    if problem == "paper":
        output["paper_id"] = "P2"
    elif problem == "missing_fact":
        output["facts"] = []
    elif problem == "extra_fact":
        output["facts"].append(source_fact("P1", "unexpected"))
    elif problem == "duplicate_fact":
        output["facts"].append(deepcopy(output["facts"][0]))
    elif problem == "changed_value":
        output["facts"][0]["value"] = "The yield was 100%."
    elif problem == "stale_verdict":
        output["facts"][0]["verification"]["input_fingerprint"] = "old"
    elif problem == "missing_source":
        paper["evidence_candidates"] = []
    else:
        row["scientific_facts"][0] = revise_fact(row["scientific_facts"][0], {"value": "A later user edit."})
    before = deepcopy(row)
    with pytest.raises((WorkflowValidationError, WorkflowConflict)):
        service.apply_fact_revision_result(row, paper, output)
    assert row == before


def test_unavailable_revision_retains_manual_candidate_and_can_be_retried(revision_service):
    service, matrix, principal = revision_service
    paper = service.fact_revision_payload(principal, "project", selected_paper_ids=["P1"])["papers"][0]
    row = deepcopy(matrix["rows"][0])
    fact = deepcopy(paper["revision_facts"][0])
    fact["verification"] = {"status": "unavailable", "input_fingerprint": review_fingerprint(fact)}
    service.apply_fact_revision_result(row, paper, {"paper_id": "P1", "facts": [fact], "error": "Provider unavailable"})
    assert row["scientific_facts"][0]["value"] == paper["revision_facts"][0]["value"]
    assert row["fact_enrichment"]["pending_revision_fact_ids"] == [fact["fact_id"]]
    assert row["fact_enrichment"]["error"] == "Provider unavailable"
