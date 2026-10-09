"""A Matrix job publishes only after every paper checkpoint is present."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.job_handlers.stage_execution import register_planning_handlers
from review_writer_api.job_service import JobYieldRequested
from review_writer_api.scientific_runner import ScientificModelDeferred


def test_matrix_job_yields_after_one_paper_and_resumes_without_publishing():
    handlers = {}
    jobs = SimpleNamespace(register_handler=lambda name, handler: handlers.update({name: handler}))
    planning = Mock()
    partial = {"source_matrix_artifact_id": "matrix-1", "completed_papers": ["P1"], "entries": {"P1": {}}}
    complete = {"source_matrix_artifact_id": "matrix-1", "completed_papers": ["P1", "P2"], "entries": {"P1": {}, "P2": {}}}
    built = iter([{"matrix_enrichment_checkpoint": partial, "papers": [{"paper_id": "P1"}]},
                  {"matrix_enrichment_checkpoint": complete, "papers": [{"paper_id": "P1"}, {"paper_id": "P2"}]}])
    register_planning_handlers(planning, jobs, {"matrix.enrich": lambda *_: next(built)})
    stored = SimpleNamespace(result={})
    repository = SimpleNamespace(get_job=lambda *_: stored, update_job_progress=Mock())
    context = SimpleNamespace(user_id="user", project_id="project", job_id="job",
                              retry_of_job_id=None, repository=repository,
                              lease_token="lease", lease_generation=1,
                              report_progress=Mock(), report_partial_result=Mock(), checkpoint=Mock())
    payload = {"source_matrix_artifact_id": "matrix-1", "pending_paper_count": 2,
               "fulltext_indexed_paper_count": 2,
               "papers": [{"paper_id": "P1"}, {"paper_id": "P2"}]}

    with pytest.raises(JobYieldRequested):
        handlers["matrix.enrich"](context, payload)
    planning.publish_matrix_enrichment.assert_not_called()
    context.report_partial_result.assert_called_once_with({"matrix_enrichment_checkpoint": partial})
    stored.result = {"matrix_enrichment_checkpoint": partial}
    handlers["matrix.enrich"](context, payload)
    assert planning.publish_matrix_enrichment.call_count == 1
    assert context.report_progress.call_args_list[1].args == (1, 2)


def test_matrix_job_waits_for_delegated_model_then_resumes_existing_checkpoint():
    handlers = {}
    jobs = SimpleNamespace(register_handler=lambda name, handler: handlers.update({name: handler}))
    planning = Mock()
    checkpoint = {"source_matrix_artifact_id": "matrix-1", "completed_papers": ["P1"],
                  "entries": {"P1": {}}}
    builder = Mock(side_effect=[ScientificModelDeferred("child-1"),
                                {"matrix_enrichment_checkpoint": checkpoint, "papers": [{"paper_id": "P1"}]}])
    register_planning_handlers(planning, jobs, {"matrix.enrich": builder})
    parent = SimpleNamespace(result={})
    child = SimpleNamespace(project_id="project", job_type="model.dispatch", status="queued",
                            error_code="")
    repository = SimpleNamespace(get_job=lambda _user, job_id: child if job_id == "child-1" else parent,
                                 update_job_progress=Mock())
    context = SimpleNamespace(user_id="user", project_id="project", job_id="parent",
        retry_of_job_id=None, lease_token="lease", lease_generation=1,
        repository=repository, report_progress=Mock(),
        report_partial_result=Mock(), checkpoint=Mock())
    payload = {"source_matrix_artifact_id": "matrix-1", "pending_paper_count": 1,
               "fulltext_indexed_paper_count": 1, "papers": [{"paper_id": "P1"}]}

    with pytest.raises(JobYieldRequested) as first:
        handlers["matrix.enrich"](context, payload)
    assert first.value.queue_reason == "model_waiting"
    assert builder.call_count == 1
    parent.result = {"waiting_model_job_id": "child-1", "matrix_enrichment_checkpoint": checkpoint}
    with pytest.raises(JobYieldRequested):
        handlers["matrix.enrich"](context, payload)
    assert builder.call_count == 1
    child.status = "succeeded"
    handlers["matrix.enrich"](context, payload)
    assert builder.call_count == 2
    assert builder.call_args.args[1]["resume_checkpoint"] == checkpoint
    planning.publish_matrix_enrichment.assert_called_once()


@pytest.mark.parametrize("ready_status", ["succeeded", "failed"])
def test_matrix_job_consumes_ready_child_while_sibling_is_still_running(ready_status):
    handlers = {}
    jobs = SimpleNamespace(register_handler=lambda name, handler: handlers.update({name: handler}))
    planning = Mock()
    parent = SimpleNamespace(result={})
    children = {key: SimpleNamespace(project_id="project", job_type="model.dispatch",
                                     status="running", error_code="")
                for key in ("slow", "ready", "replacement")}
    repository = SimpleNamespace(get_job=lambda _user, key: parent if key == "parent" else children[key],
                                 update_job_progress=Mock())
    context = SimpleNamespace(user_id="user", project_id="project", job_id="parent",
        retry_of_job_id=None, lease_token="lease", lease_generation=1, repository=repository,
        report_progress=Mock(), checkpoint=Mock())
    context.report_partial_result = lambda result: setattr(parent, "result", deepcopy(result))
    initial = {"source_matrix_artifact_id": "matrix", "completed_papers": [], "entries": {}}
    advanced = {"source_matrix_artifact_id": "matrix", "completed_papers": ["P2"],
                "entries": {"P2": {"result": {"paper_id": "P2", "status": ready_status}}}}
    calls = []

    def build(ctx, payload):
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            ctx.report_partial_result({"matrix_enrichment_checkpoint": initial})
            raise ScientificModelDeferred("slow", model_job_ids=["slow", "ready"])
        assert payload["resume_checkpoint"] == initial
        ctx.report_partial_result({"matrix_enrichment_checkpoint": advanced})
        raise ScientificModelDeferred("slow", model_job_ids=["slow", "replacement"])

    register_planning_handlers(planning, jobs, {"matrix.enrich": build})
    payload = {"source_matrix_artifact_id": "matrix", "pending_paper_count": 3,
               "fulltext_indexed_paper_count": 3,
               "papers": [{"paper_id": f"P{i}"} for i in range(1, 4)]}
    handler = handlers["matrix.enrich"]
    with pytest.raises(JobYieldRequested):
        handler(context, payload)
    assert parent.result["waiting_model_job_ids"] == ["slow", "ready"]
    with pytest.raises(JobYieldRequested):
        handler(context, payload)
    assert len(calls) == 1

    children["ready"].status = ready_status
    with pytest.raises(JobYieldRequested):
        handler(context, payload)
    assert len(calls) == 2
    assert parent.result["matrix_enrichment_checkpoint"] == advanced
    assert parent.result["waiting_model_job_ids"] == ["slow", "replacement"]
    planning.publish_matrix_enrichment.assert_not_called()
