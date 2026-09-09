"""Offline Stage 03/04 boundary tests using disposable artifacts, never user data."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.job_handlers.stage_execution import queue_fact_revision, register_planning_handlers
from review_writer_api.tests import test_planning_v1 as planning_fixture
from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION, fact_usage
from review_writer_core.stages.planning.fact_revision import revise_fact, review_fingerprint
from review_writer_core.stages.planning.matrix import _json_bytes
from review_writer_core.stages.planning.routing import current_classification_tags, routing_facts


def fact(fid="original", value="The protocol gave 91% yield."):
    quote = "The protocol gave 91% yield and 96% selectivity."
    ref = {"evidence_key": "source", "paper_id": "P001", "source_file_id": "P001",
           "chunk_id": "results", "source_lineage_hash": "lineage", "support_excerpt": quote}
    return {"fact_id": fid, "paper_id": "P001", "field_id": "quantitative_results", "value": value,
            "subject": "old subject", "qualifiers": {"object": "old"}, "normalized_value": "old value",
            "evidence_refs": [ref], "support_spans": [ref], "support_excerpt": quote,
            "evidence_ceiling": "Only the reported experiment.", "epistemic_status": "direct_source_report",
            "support_level": "direct", "assertion_ceiling": "direct_source_report",
            "validation_contract": FACT_VALIDATION_VERSION,
            "verification": {"status": "supported", "contract": FACT_VALIDATION_VERSION}}


def test_revision_identity_and_semantic_fields_do_not_reuse_old_support():
    old = fact()
    new = revise_fact(old, {"value": "The product was obtained in 91% yield."})
    assert new["fact_id"] != old["fact_id"]
    assert new["revision_of_fact_id"] == old["fact_id"]
    assert new["qualifiers"] == {} and new["subject"] == ""
    assert new["normalized_value"] == new["value"]
    assert fact_usage(new) == "unusable"
    assert new["verification"]["input_fingerprint"] == review_fingerprint(new)
    assert old["verification"]["status"] == "supported"
    assert revise_fact(old, old) == old
    assert revise_fact(old, {"evidence_ceiling": "Only a qualitative report."})["fact_id"] != old["fact_id"]


def test_formula_internal_whitespace_is_not_treated_as_the_same_review_input():
    old = fact(value="A  B")
    assert revise_fact(old, {"value": "A B"})["fact_id"] != old["fact_id"]


@pytest.mark.parametrize("status", ["pending", "rejected", "uncertain", "unavailable"])
def test_invalid_classification_never_becomes_current_routing(status):
    candidate = {**fact(), "fact_type": "classification", "field_id": "topic_partition",
                 "verification": {"status": status}}
    row = {"scientific_facts": [candidate], "evidence_backed_tags": {"objects": [{
        "fact_ids": [candidate["fact_id"]], "partition_label": "Group B"}]}}
    assert fact_usage(candidate) == "unusable"
    assert routing_facts(row) == [] and current_classification_tags(row) == {}


def test_enqueue_failure_preserves_saved_edit_and_retry_instruction():
    saved = {"matrix_artifact_id": "new", "pending_fact_revisions": ["fact"]}
    jobs = Mock()
    jobs.submit.side_effect = RuntimeError("queue unavailable")
    result = queue_fact_revision(None, jobs, None, "project", saved)
    assert result["matrix_artifact_id"] == "new"
    assert result["fact_verification"]["status"] == "pending"
    assert result["fact_verification"]["retryable"] is True
    jobs.reset_mock()
    queue_fact_revision(None, jobs, None, "project", {"pending_fact_revisions": []})
    jobs.submit.assert_not_called()


def test_matrix_stale_job_is_rejected_before_builder_or_source_preparation():
    service, jobs, builder = Mock(), Mock(), Mock()
    service.validate_matrix_enrichment_inputs.side_effect = WorkflowConflict("stale")
    registered = {}
    jobs.register_handler.side_effect = lambda name, handler: registered.update({name: handler})
    register_planning_handlers(service, jobs, {"matrix.enrich": builder})
    with pytest.raises(WorkflowConflict):
        registered["matrix.enrich"](Mock(user_id="user", project_id="project"),
                                     {"prepare_on_start": True, "operation": "fact_revision"})
    builder.assert_not_called()
    service.fact_revision_payload.assert_not_called()


@pytest.fixture
def matrix_env():
    env = planning_fixture.PlanningV1Tests()
    env.setUp()
    service = env.app.state.planning_service
    matrix, _ = service._matrix(env.first, env.project_id)
    matrix = deepcopy(matrix)
    row = next(row for row in matrix["rows"] if row["paper_id"] == "P001")
    row["scientific_facts"] = [fact(), fact("untouched", "The protocol gave 96% selectivity.")]
    row["fact_enrichment"] = {"status": "complete", "required_fact_roles": ["quantitative_results"]}
    state = service.repository.get_stage_state(env.first.user_id, env.project_id, "matrix")
    published, run = service._publish_files(env.first, env.project_id, stage_id="matrix",
        files={"matrix/literature_matrix.json": (_json_bytes(matrix), "json")}, input_snapshot={"test": True})
    service.repository.promote_stage_artifacts_atomically(env.first.user_id, env.project_id, "matrix",
        artifact_ids={"matrix/literature_matrix.json": published["matrix/literature_matrix.json"].id},
        run_id=run.id, expected_revision=state.revision, status="review")
    service.library_index = SimpleNamespace(enabled=True,
        summaries=lambda _principal, ids: {sid: {"fulltext": "ready", "source_lineage_hash": "lineage"} for sid in ids},
        resolve_source_chunks=lambda *_args, **_kwargs: [{"chunk_id": "results", "content": fact()["support_excerpt"],
            "content_type": "text", "source_lineage_hash": "lineage"}])
    try:
        yield env, service
    finally:
        env.tearDown()


def edit(env, service, value="The product was obtained in 91% yield."):
    matrix, _ = service._matrix(env.first, env.project_id)
    row = next(row for row in matrix["rows"] if row["paper_id"] == "P001")
    edits = deepcopy(row["scientific_facts"])
    edits[0]["value"] = value
    revision = service.repository.get_stage_state(env.first.user_id, env.project_id, "matrix").revision
    return service.update_matrix_row(env.first, env.project_id, "P001", revision=revision,
        main_content=None, most_relevant_figure=None, scientific_facts=edits, mark_complete=False)


def run_revision(payload, tmp_path, monkeypatch, model):
    path = Path(__file__).resolve().parents[1] / "skills/review-literature-matrix-outline/scripts/enrich_matrix_facts.py"
    spec = importlib.util.spec_from_file_location("fact_revision_integration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, output, progress, checkpoint = [tmp_path / name for name in ("input.json", "output.json", "progress.json", "checkpoint.json")]
    module.write_json(source, payload)
    monkeypatch.setattr(module, "call_json_model", model)
    monkeypatch.setattr("sys.argv", [str(path), "--input", str(source), "--output", str(output),
                                    "--progress", str(progress), "--checkpoint", str(checkpoint)])
    assert module.main() == 0
    return json.loads(output.read_text(encoding="utf-8"))


def test_edit_verification_publish_uses_only_changed_facts(matrix_env, tmp_path, monkeypatch):
    env, service = matrix_env
    saved = edit(env, service)
    assert saved["row"]["fact_enrichment"]["status"] == "pending"
    payload = service.matrix_enrichment_payload(env.first, env.project_id)
    assert payload["operation"] == "fact_revision"
    targets = payload["papers"][0]["revision_facts"]
    assert len(payload["papers"]) == len(targets) == 1
    model = Mock(return_value={"verdicts": [{"fact_id": targets[0]["fact_id"], "status": "supported", "reason": "Same experiment and yield."}]})
    result = run_revision(payload, tmp_path, monkeypatch, model)
    assert model.call_count == 1
    assert "untouched" not in model.call_args.args[0]
    service.publish_matrix_enrichment(env.first, env.project_id, payload, result)
    matrix, _ = service._matrix(env.first, env.project_id)
    current = next(row for row in matrix["rows"] if row["paper_id"] == "P001")
    assert current["scientific_facts"][1] == fact("untouched", "The protocol gave 96% selectivity.")
    assert fact_usage(current["scientific_facts"][0]) == "direct"
    assert current["fact_enrichment"]["status"] == "complete"
    # An unchanged candidate/checkpoint needs no second paid verification.
    run_revision(payload, tmp_path, monkeypatch, model)
    assert model.call_count == 1


def test_old_verdict_cannot_overwrite_second_edit(matrix_env):
    env, service = matrix_env
    edit(env, service)
    payload = service.matrix_enrichment_payload(env.first, env.project_id)
    edit(env, service, "The study reports a product yield of 91%.")
    with pytest.raises(WorkflowConflict):
        service.publish_matrix_enrichment(env.first, env.project_id, payload, {})


def test_unavailable_verifier_does_not_restore_old_supported_status(matrix_env, tmp_path, monkeypatch):
    env, service = matrix_env
    edit(env, service)
    payload = service.matrix_enrichment_payload(env.first, env.project_id)
    result = run_revision(payload, tmp_path, monkeypatch, Mock(side_effect=RuntimeError("offline")))
    service.publish_matrix_enrichment(env.first, env.project_id, payload, result)
    retry = service.matrix_enrichment_payload(env.first, env.project_id)
    assert retry["operation"] == "fact_revision"
    assert fact_usage(retry["papers"][0]["revision_facts"][0]) == "unusable"


def test_router_construction_no_longer_registers_stage_jobs():
    from review_writer_api.routers.planning import build_planning_router
    from review_writer_api.routers.sections import build_sections_router
    jobs = Mock()
    build_planning_router(lambda: None, Mock(), jobs)
    build_sections_router(lambda: None, Mock(), jobs)
    jobs.register_handler.assert_not_called()


def test_verified_classification_can_realign_but_not_override_human_choice(matrix_env):
    env, service = matrix_env
    classified = {**fact(), "fact_type": "classification", "field_id": "topic_partition"}
    row = {"paper_id": "P001", "scientific_facts": [classified], "evidence_backed_tags": {"custom-axis": [{
        "fact_ids": [classified["fact_id"]], "partition_label": "Independent study design"}]}}
    sections = [{"title": "Old candidate", "section_role": "body", "paper_ids": ["P001"]}]
    def realign():
        return service._realign_generated_body_sections(sections, [row], {"P001": "Distracting category words"},
            outline_style="topic-guided", taxonomy_profile="chemistry_general", tag_key_override="custom-axis")
    repaired, changes = realign()
    assert repaired[0]["title"] == "Independent study design" and changes
    row["human_confirmed_tags"] = {"custom-axis": ["Old candidate"]}
    assert realign() == (sections, [])
    row.pop("human_confirmed_tags")
    classified["verification"] = {"status": "rejected"}
    assert realign() == (sections, [])


def test_zero_query_hits_are_not_reported_as_missing_index():
    from review_writer_core.stages.planning.matrix import has_fact_sources
    assert has_fact_sources({"fulltext_indexed_paper_count": 2, "fulltext_candidate_paper_count": 0})
    assert not has_fact_sources({"fulltext_indexed_paper_count": 0, "fulltext_candidate_paper_count": 0})


def test_rejected_classification_is_not_counted_as_a_scientific_fact():
    from review_writer_core.review_fact_readiness import fact_readiness_report
    classified = {**fact(), "fact_type": "classification", "verification": {"status": "rejected"}}
    report = fact_readiness_report(facts=[classified], required_roles=[], extraction_status="partial")
    assert report["scientific_fact_count"] == report["usable_fact_count"] == 0
    assert report["classification_fact_count"] == 1


def test_tampered_review_or_duplicate_fact_ids_cannot_publish(matrix_env):
    env, service = matrix_env
    edit(env, service)
    payload = service.matrix_enrichment_payload(env.first, env.project_id)
    reviewed = deepcopy(payload["papers"][0]["revision_facts"])
    reviewed[0]["value"] = "An unrelated experiment worked."
    with pytest.raises(WorkflowConflict):
        service.publish_matrix_enrichment(env.first, env.project_id, payload, {"papers": [{"paper_id": "P001", "facts": reviewed}]})
    matrix, _ = service._matrix(env.first, env.project_id)
    current = next(row for row in matrix["rows"] if row["paper_id"] == "P001")
    revision = service.repository.get_stage_state(env.first.user_id, env.project_id, "matrix").revision
    with pytest.raises(WorkflowValidationError):
        service.update_matrix_row(env.first, env.project_id, "P001", revision=revision, main_content=None,
            most_relevant_figure=None, scientific_facts=[*current["scientific_facts"], current["scientific_facts"][0]], mark_complete=False)


def test_changed_registered_context_invalidates_revision_checkpoint(matrix_env):
    env, service = matrix_env
    edit(env, service)
    before = service.matrix_enrichment_payload(env.first, env.project_id)
    original = service.library_index.resolve_source_chunks
    service.library_index.resolve_source_chunks = lambda *args, **kwargs: [
        {**row, "content": row["content"] + " Additional experiment context."} for row in original(*args, **kwargs)]
    after = service.matrix_enrichment_payload(env.first, env.project_id)
    assert before["papers"][0]["source_fingerprint"] != after["papers"][0]["source_fingerprint"]


def test_http_edit_queues_revision_but_reading_note_does_not(matrix_env, monkeypatch):
    from fastapi.testclient import TestClient
    env, service = matrix_env
    submit = Mock(return_value=SimpleNamespace(id="queued-job"))
    monkeypatch.setattr(env.app.state.job_service, "submit", submit)
    matrix, _ = service._matrix(env.first, env.project_id)
    row = next(row for row in matrix["rows"] if row["paper_id"] == "P001")
    edits = deepcopy(row["scientific_facts"])
    edits[0]["value"] = "A product yield of 91% was reported."
    revision = service.repository.get_stage_state(env.first.user_id, env.project_id, "matrix").revision
    with TestClient(env.app) as client:
        response = client.put(f"/api/v1/projects/{env.project_id}/planning/matrix/P001", headers=env.headers(),
            json={"revision": revision, "scientific_facts": edits, "mark_complete": False})
        assert response.status_code == 200, response.text
        assert response.json()["fact_verification"]["status"] == "queued"
        assert submit.call_args.kwargs["payload"]["operation"] == "fact_revision"
        submit.reset_mock()
        response = client.put(f"/api/v1/projects/{env.project_id}/planning/matrix/P001", headers=env.headers(),
            json={"revision": response.json()["matrix_revision"], "main_content": "A reading note", "mark_complete": False})
        assert response.status_code == 200, response.text
        submit.assert_not_called()


def test_exact_source_resolver_checks_owner_version_and_chunk(matrix_env):
    import uuid
    from sqlalchemy import select
    from review_writer_api.domain_services.library_index import LibraryIndexService
    from review_writer_api.workflow_models import LibraryPaper, LibraryDocumentIndex, LibraryDocumentChunk
    env, service = matrix_env
    with env.sessions.begin() as session:
        paper = session.scalar(select(LibraryPaper).where(LibraryPaper.user_id == uuid.UUID(env.first.user_id), LibraryPaper.paper_id == "P001"))
        index = LibraryDocumentIndex(library_paper_id=paper.id, user_id=paper.user_id, paper_id="P001",
            source_lineage_hash="current-lineage", chunker_version="test", status="ready", is_current=True)
        session.add(index)
        session.flush()
        session.add(LibraryDocumentChunk(index_id=index.id, user_id=paper.user_id, paper_id="P001",
            chunk_id="registered-chunk", ordinal=0, content="Exact original content.",
            normalized_content="exact original content", block_start=0, block_end=0))
    resolver = SimpleNamespace(session_factory=env.sessions)
    def get(principal, lineage="current-lineage", ids=None):
        return LibraryIndexService.resolve_source_chunks(resolver, principal, "P001", ids or ["registered-chunk"], expected_lineage=lineage)
    assert get(env.first)[0]["content"] == "Exact original content."
    assert get(env.second) == []
    assert get(env.first, "old-lineage") == []
    assert get(env.first, ids=["other"]) == []
