from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.job_service import JobYieldRequested
from review_writer_api.job_handlers.stage_execution import (
    register_planning_handlers, _yield_incomplete_matrix_batch, _planning_fact_checkpoint,
)


def setup_handler():
    service, jobs, planner = Mock(), Mock(), Mock()
    state = {}
    context = Mock(user_id="owner", project_id="project", job_id="current", retry_of_job_id=None)
    context.repository.get_job.side_effect = lambda user, job: SimpleNamespace(result=deepcopy(state))
    context.report_partial_result.side_effect = lambda result: state.update(deepcopy(result))
    source = {"source_matrix_artifact_id": "matrix", "pending_paper_count": 3, "fulltext_indexed_paper_count": 3,
              "papers": [{"paper_id": p, "source_fingerprint": p,
                          "evidence_candidates": [{"content": "source"}]} for p in ("P1", "P2", "P3")]}
    service.resume_blueprint_after_matrix.side_effect = lambda user, project, payload: payload
    service.matrix_enrichment_payload.side_effect = lambda *a, **kw: deepcopy(source)
    service.publish_matrix_enrichment.return_value = {"matrix_snapshot": {"rows": []}}
    planner.return_value = {"blueprint_checkpoint": {}}
    service.publish_blueprint_candidate.return_value = {"section_blueprint": {"source_matrix_artifact_id": "matrix"}}
    processed = []

    def enrich(ctx, payload):
        checkpoint = deepcopy(payload.get("resume_checkpoint") or {})
        done = checkpoint.get("completed_papers", [])
        remaining = [p["paper_id"] for p in payload["papers"] if p["paper_id"] not in done]
        if remaining:
            done.append(remaining[0])
            processed.append(remaining[0])
        return {"papers": [{"paper_id": p} for p in done],
                "matrix_enrichment_checkpoint": {"source_matrix_artifact_id": "matrix", "completed_papers": done}}

    register_planning_handlers(service, jobs, {"matrix.enrich": enrich, "planning.blueprint": planner})
    handler = next(c.args[1] for c in jobs.register_handler.call_args_list if c.args[0] == "planning.blueprint")
    payload = {"integrated_fact_enrichment": {"enabled": True, "source_matrix_artifact_id": "matrix"},
               "section_blueprint": {"source_matrix_artifact_id": "matrix", "sections": []}}
    return handler, context, payload, service, planner, processed, state


def test_three_batches_resume_current_job_and_publish_only_when_complete():
    handler, ctx, payload, service, planner, processed, state = setup_handler()
    for expected in (1, 2):
        with pytest.raises(JobYieldRequested):
            handler(ctx, payload)
        assert len(state["matrix_enrichment_checkpoint"]["completed_papers"]) == expected
        service.publish_matrix_enrichment.assert_not_called()
        planner.assert_not_called()
    assert handler(ctx, payload)["planning_pipeline"]["phase"] == "completed"
    assert processed == ["P1", "P2", "P3"]
    service.publish_matrix_enrichment.assert_called_once()
    assert service.publish_matrix_enrichment.call_args.kwargs["candidate_only"] is True


def test_changed_matrix_stops_before_builder():
    handler, ctx, payload, service, planner, processed, state = setup_handler()
    payload["integrated_fact_enrichment"]["source_matrix_artifact_id"] = "old"
    with pytest.raises(WorkflowConflict):
        handler(ctx, payload)
    assert not processed
    service.publish_matrix_enrichment.assert_not_called()


def test_retry_checkpoint_then_same_job_checkpoint_takes_precedence():
    ctx = Mock(user_id="user", job_id="current", retry_of_job_id="previous")
    results = {"current": {}, "previous": {"matrix_enrichment_checkpoint": {"completed_papers": ["P1"]}}}
    ctx.repository.get_job.side_effect = lambda u, j: SimpleNamespace(result=results[j])
    assert _planning_fact_checkpoint(ctx, {})["completed_papers"] == ["P1"]
    results["current"] = {"matrix_enrichment_checkpoint": {"completed_papers": ["P1", "P2"]}}
    assert _planning_fact_checkpoint(ctx, {})["completed_papers"] == ["P1", "P2"]


@pytest.mark.parametrize("returned,completed,matrix", [
    (["foreign"], ["foreign"], "matrix"), (["P1"], ["P1", "P2"], "matrix"),
    (["P1"], ["P1"], "old"),
])
def test_invalid_batch_is_not_silently_requeued(returned, completed, matrix):
    ctx = Mock()
    with pytest.raises(WorkflowValidationError):
        _yield_incomplete_matrix_batch(ctx,
            {"source_matrix_artifact_id": "matrix", "papers": [{"paper_id": "P1"}, {"paper_id": "P2"}]},
            {"papers": [{"paper_id": p} for p in returned],
             "matrix_enrichment_checkpoint": {"source_matrix_artifact_id": matrix, "completed_papers": completed}})
    ctx.report_partial_result.assert_not_called()
