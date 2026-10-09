"""Offline navigation/source and immutable planning snapshot regressions."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.domain_services.base import OwnedProjectService
from review_writer_api.domain_services.library_index import EvidenceHit
from review_writer_api.job_service import JobYieldRequested
from review_writer_api.job_handlers.stage_execution import register_planning_handlers
from review_writer_api.planning_jobs import queue_matrix_enrichment
from review_writer_api.tests.test_planning_fact_batches import setup_handler
from review_writer_core.review_fact_readiness import REVIEW_READING_STRATEGY
from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION
from review_writer_core.source_attribution import contribution_context, navigation_source_refs
from review_writer_core.stages.planning.academic_planning import _structure_contributions


def source_ref(key="source", *, source="P1", lineage="v1", chunk="own-result"):
    return {"paper_id": "P1", "source_file_id": source, "chunk_id": chunk,
            "source_lineage_hash": lineage, "evidence_key": key}


def test_navigation_refs_remain_locators_and_project_relation_is_not_reused():
    row = {"paper_id": "P1", "paper_analysis": {"research_question": "How does it work?",
        "contribution": "A bounded finding", "topic_relation": "Relates to old project",
        "review_topic": "Old project", "evidence_keys": ["source", "source"],
        "evidence_refs": [source_ref()]}, "scientific_facts": [
            {"evidence_refs": [source_ref(), {"evidence_key": "unlocatable"}]}]}
    before = deepcopy(row)
    assert navigation_source_refs(row) == [source_ref()]
    context = contribution_context(row["paper_analysis"], review_topic="New project")
    assert context["evidence_keys"] == ["source"]
    assert context["topic_relation"] == ""
    assert context["usage"] == "navigation_only"
    assert row == before
    legacy = _structure_contributions([row], 4000, review_topic="New project")[0]
    assert legacy["paper_analysis"]["topic_relation"] == "Relates to old project"
    assert "evidence_keys" not in legacy["paper_analysis"]
    assert "candidate_findings" not in legacy


def test_planning_restores_current_originals_even_with_abstract_and_keeps_matrix_clean():
    calls = []

    class Index:
        enabled = True

        def summaries(self, principal, ids):
            return {pid: {"source_lineage_hash": "v1", "fulltext": "ready"} for pid in ids}

        def resolve_source_chunks(self, principal, sid, chunks, *, expected_lineage):
            calls.append((sid, chunks, expected_lineage))
            return [{"paper_id": sid, "chunk_id": "own-result", "source_lineage_hash": "v1",
                "page_start": 3, "content_type": "text", "content": "This study reports its own measured result."}]

        def primary_contribution_hits(self, principal, **kwargs):
            return [EvidenceHit(paper_id="P1", chunk_id="body", content="The measured result was reproduced.",
                page_start=4, page_end=4, section_path=("Results",), content_type="text", asset_refs=(),
                score=1.0, match_reason="primary_contribution_recovery", is_neighbor=False,
                index_id="index", source_lineage_hash="v1")]

    service = SimpleNamespace(library_index=Index(), _matrix_abstract=PlanningService._matrix_abstract,
        _catalog=lambda principal, ids: {"P1": SimpleNamespace(metadata_json={})},
        _supporting_source_parents=OwnedProjectService._supporting_source_parents)
    row = {"paper_id": "P1", "abstract": "A perfectly usable abstract.",
        "paper_analysis": {"contribution": "A contribution", "evidence_keys": ["source"],
            "evidence_refs": [source_ref(), source_ref("foreign", source="P2"),
                              source_ref("stale", lineage="old", chunk="stale")]},
        "scientific_facts": [{"fact_id": "candidate", "value": "A candidate result",
            "validation_contract": FACT_VALIDATION_VERSION, "verification": {"status": "pending"},
            "evidence_refs": [source_ref()]}]}
    matrix = {"rows": [row]}
    before = deepcopy(matrix)
    projected = PlanningService.planning_matrix_with_sources(service, None, matrix,
                                                           reading_strategy=REVIEW_READING_STRATEGY)
    assert calls == [("P1", ["own-result"], "v1")]
    passages = projected["rows"][0]["planning_source_passages"]
    assert {p["chunk_id"] for p in passages} == {"own-result", "body"}
    assert matrix == before
    context = _structure_contributions(projected["rows"], 4000, review_topic="Current topic",
                                       reading_strategy=REVIEW_READING_STRATEGY)[0]
    assert context["abstract"]
    assert context["source_passages"][0]["content"]
    assert context["verified_facts"] == []
    assert context["candidate_findings"][0]["usage"] == "unverified_navigation_only"
    assert context["candidate_findings"][0]["evidence_keys"] == ["source"]


def test_integrated_reading_refreshes_new_model_snapshot_without_mutating_request():
    handler, ctx, payload, service, planner, processed, state = setup_handler()
    payload["reading_strategy"] = REVIEW_READING_STRATEGY
    payload["section_blueprint"]["reading_strategy"] = REVIEW_READING_STRATEGY
    payload["planning_matrix_snapshot"] = {"rows": [{"paper_id": "old-context"}]}
    before = deepcopy(payload)
    refreshed = {"rows": [{"paper_id": "P1", "planning_source_passages": [{"content": "New source."}]}]}
    service.planning_matrix_with_sources.return_value = refreshed
    for _ in range(2):
        with pytest.raises(JobYieldRequested):
            handler(ctx, payload)
    handler(ctx, payload)
    service.planning_matrix_with_sources.assert_called_once()
    assert service.matrix_enrichment_payload.call_args.kwargs["reading_strategy"] == REVIEW_READING_STRATEGY
    model_input = planner.call_args.args[-1]
    assert model_input["planning_matrix_snapshot"] == refreshed
    assert model_input["matrix_snapshot"] == {"rows": []}
    assert payload == before


def test_legacy_inflight_planning_does_not_adopt_new_reading_strategy():
    handler, ctx, payload, service, planner, processed, state = setup_handler()
    for _ in range(2):
        with pytest.raises(JobYieldRequested):
            handler(ctx, payload)
    handler(ctx, payload)
    assert service.matrix_enrichment_payload.call_args.kwargs["reading_strategy"] is None
    service.planning_matrix_with_sources.assert_not_called()


def test_long_abstract_and_verified_fact_cannot_crowd_out_originals():
    row = {"paper_id": "P1", "abstract": "Abstract " * 1000,
        "scientific_facts": [{"fact_id": "verified-legacy", "value": "Long fact " * 1000,
            "field_id": "object_input", "support_level": "direct", "evidence_refs": [{"evidence_key": "old"}]}],
        "planning_source_passages": [{"evidence_key": "source", "content": "Original body " * 100}]}
    context = _structure_contributions([row], 600, reading_strategy=REVIEW_READING_STRATEGY)[0]
    assert context["source_passages"][0]["content"].startswith("Original body")
    assert len(context["source_passages"][0]["content"]) > len(context["abstract"])


@pytest.mark.parametrize("paper_ids", [None, [], ["P2", "P1", "P2"]])
@pytest.mark.parametrize("force", [False, True])
def test_fresh_matrix_analysis_uses_review_reading_independent_of_paper_scope(paper_ids, force):
    """Selecting unfinished papers changes scope, not the scientific task purpose."""
    planning, jobs = Mock(), Mock()
    principal = SimpleNamespace(user_id="owner")
    planning._matrix.return_value = ({}, SimpleNamespace(id="matrix"))
    jobs.repository.get_current_job.return_value = None

    queue_matrix_enrichment(planning, jobs, principal, "project", paper_ids=paper_ids, force=force)

    request = jobs.submit.call_args.kwargs["payload"]
    assert request["reading_strategy"] == REVIEW_READING_STRATEGY
    assert request["selected_paper_ids"] == sorted(set(paper_ids or []))
    assert request["force_refresh"] is force
    assert request["source_matrix_artifact_id"] == "matrix"
    assert "resume_from_job_id" not in request
    planning.matrix_enrichment_payload.assert_not_called()  # Submission itself remains free.


@pytest.mark.parametrize("previous_strategy", [None, REVIEW_READING_STRATEGY])
@pytest.mark.parametrize("checkpoint_key", ["matrix_enrichment_checkpoint", "section_checkpoint"])
def test_scoped_checkpoint_recovery_keeps_the_original_reading_contract(previous_strategy, checkpoint_key):
    planning, jobs = Mock(), Mock()
    principal = SimpleNamespace(user_id="owner")
    planning._matrix.return_value = ({}, SimpleNamespace(id="matrix"))
    previous_payload = {"source_matrix_artifact_id": "matrix", "selected_paper_ids": ["P1", "P2"]}
    if previous_strategy is not None:
        previous_payload["reading_strategy"] = previous_strategy
    current = SimpleNamespace(id="previous", status="failed", payload=previous_payload,
        result={checkpoint_key: {"completed_papers": ["P1"]}})
    jobs.repository.get_current_job.return_value = current
    before = deepcopy(previous_payload)

    queue_matrix_enrichment(planning, jobs, principal, "project", paper_ids=["P2"])

    request = jobs.submit.call_args.kwargs["payload"]
    assert request["resume_from_job_id"] == "previous"
    assert request.get("reading_strategy") == previous_strategy
    assert current.payload == before


def test_fresh_scoped_request_does_not_inherit_legacy_mode_without_a_checkpoint():
    planning, jobs = Mock(), Mock()
    planning._matrix.return_value = ({}, SimpleNamespace(id="matrix"))
    jobs.repository.get_current_job.return_value = SimpleNamespace(id="finished", status="succeeded",
        payload={"source_matrix_artifact_id": "matrix"}, result={"matrix_artifact_id": "matrix"})

    queue_matrix_enrichment(planning, jobs, SimpleNamespace(user_id="owner"), "project", paper_ids=["P1"])

    request = jobs.submit.call_args.kwargs["payload"]
    assert request["reading_strategy"] == REVIEW_READING_STRATEGY
    assert "resume_from_job_id" not in request


def test_scoped_worker_forwards_review_reading_and_reuses_current_results_without_a_model_call():
    planning, jobs, builder = Mock(), Mock(), Mock()
    planning.matrix_enrichment_payload.return_value = {
        "source_matrix_artifact_id": "matrix", "pending_paper_count": 0,
        "reading_strategy": REVIEW_READING_STRATEGY,
    }
    register_planning_handlers(planning, jobs, {"matrix.enrich": builder})
    handler = jobs.register_handler.call_args.args[1]
    context = Mock(user_id="owner", project_id="project", job_id="current", retry_of_job_id=None)
    context.repository.get_job.return_value = SimpleNamespace(result={})
    request = {"prepare_on_start": True, "source_matrix_artifact_id": "matrix",
        "selected_paper_ids": ["P1"], "reading_strategy": REVIEW_READING_STRATEGY}
    before = deepcopy(request)

    result = handler(context, request)

    assert result["status"] == "current"
    assert planning.matrix_enrichment_payload.call_args.kwargs["selected_paper_ids"] == ["P1"]
    assert planning.matrix_enrichment_payload.call_args.kwargs["reading_strategy"] == REVIEW_READING_STRATEGY
    assert request == before
    builder.assert_not_called()
    planning.publish_matrix_enrichment.assert_not_called()


def test_explicit_fact_revision_still_uses_its_strict_payload_not_ordinary_reading():
    planning, jobs, builder = Mock(), Mock(), Mock()
    revision = {"source_matrix_artifact_id": "matrix", "operation": "fact_revision",
        "pending_paper_count": 1, "fulltext_indexed_paper_count": 1, "papers": [{
            "paper_id": "P1", "verify_only": True, "required_fact_roles": ["quantitative_results"],
            "evidence_candidates": [{"content": "The measured source result."}],
        }]}
    planning.fact_revision_payload.return_value = revision
    builder.return_value = {"papers": [{"paper_id": "P1"}], "matrix_enrichment_checkpoint": {
        "source_matrix_artifact_id": "matrix", "completed_papers": ["P1"]}}
    register_planning_handlers(planning, jobs, {"matrix.enrich": builder})
    handler = jobs.register_handler.call_args.args[1]
    context = Mock(user_id="owner", project_id="project", job_id="current", retry_of_job_id=None)
    context.repository.get_job.return_value = SimpleNamespace(result={})

    handler(context, {"prepare_on_start": True, "source_matrix_artifact_id": "matrix",
        "operation": "fact_revision", "reading_strategy": REVIEW_READING_STRATEGY})

    planning.matrix_enrichment_payload.assert_not_called()
    assert builder.call_args.args[1] == revision
    assert "reading_strategy" not in builder.call_args.args[1]
    assert builder.call_args.args[1]["papers"][0]["verify_only"] is True


@pytest.mark.parametrize("scoped", [False, True])
def test_mixed_reading_keeps_manual_verification_and_current_cache_separate(scoped):
    """Offline first-read, edited-fact and existing-artifact flows share no fake promotion."""
    from review_writer_api.tests.test_planning_v1 import PlanningV1Tests
    from review_writer_core.scientific_facts import review_fingerprint
    from review_writer_core.stages.planning.fact_revision import revise_fact

    fixture = PlanningV1Tests("test_review_reading_source_key_ignores_topic_model_but_not_source_changes")
    fixture.setUp()
    try:
        service = fixture.app.state.planning_service
        matrix, artifact = service._matrix(fixture.first, fixture.project_id)
        snapshot = {**deepcopy(matrix), "rows": deepcopy(matrix["rows"][:3])}
        selected = [row["paper_id"] for row in snapshot["rows"]]
        initial = service.matrix_enrichment_payload(fixture.first, fixture.project_id, selected_paper_ids=selected)
        inputs = {paper["paper_id"]: paper for paper in initial["papers"]}
        edited_row, fresh_row, cached_row = snapshot["rows"]
        source = inputs[edited_row["paper_id"]]["evidence_candidates"][0]
        old = {"fact_id": "original", "paper_id": edited_row["paper_id"], "field_id": "object_input",
            "value": source["content"], "evidence_ceiling": "Only this source.",
            "assertion_ceiling": "abstract_report_only", "support_level": "abstract_limited",
            "validation_contract": FACT_VALIDATION_VERSION,
            "evidence_refs": [{**source, "source_file_id": source["paper_id"],
                "support_excerpt": source["content"]}]}
        old["verification"] = {"status": "supported", "contract": FACT_VALIDATION_VERSION,
            "input_fingerprint": review_fingerprint(old)}
        revision = revise_fact(old, {"value": "A user-edited claim that still needs source verification."})
        edited_row["scientific_facts"] = [old, revision]
        cached_source = inputs[cached_row["paper_id"]]
        cached_row["fact_enrichment"] = {"status": "complete", "fact_extraction_profile": {
            "reading_strategy": REVIEW_READING_STRATEGY, "reading_status": "completed",
            "reading_source_key": cached_source["reading_source_key"]}}
        cached_row["paper_analysis"] = {"contribution": "Already read; do not pay for a second reading."}
        before = deepcopy(snapshot)

        with patch.object(service, "_matrix", return_value=(snapshot, artifact)):
            payload = service.matrix_enrichment_payload(fixture.first, fixture.project_id,
                selected_paper_ids=selected if scoped else None)
            assert payload["reading_strategy"] == REVIEW_READING_STRATEGY
            assert payload["pending_paper_count"] == 2
            papers = {paper["paper_id"]: paper for paper in payload["papers"]}
            assert set(papers) == {edited_row["paper_id"], fresh_row["paper_id"]}
            edited = papers[edited_row["paper_id"]]
            fresh = papers[fresh_row["paper_id"]]
            assert edited["verify_only"] is True
            assert edited["reading_strategy"] != REVIEW_READING_STRATEGY
            assert edited["revision_facts"] == [revision]
            assert fresh["reading_strategy"] == REVIEW_READING_STRATEGY
            assert not fresh.get("verify_only")

            rejected = deepcopy(revision)
            rejected["verification"] = {"status": "unsupported", "contract": FACT_VALIDATION_VERSION,
                "input_fingerprint": review_fingerprint(revision), "reason": "Offline source verdict."}
            built = {"papers": [{"paper_id": edited_row["paper_id"], "status": "partial", "facts": [rejected]},
                {"paper_id": fresh_row["paper_id"], "status": "complete", "facts": [],
                    "paper_analysis": {"contribution": "A source-located contribution.",
                        "evidence_keys": [fresh["evidence_candidates"][0]["evidence_key"]]},
                    "fact_extraction_profile": {"reading_strategy": REVIEW_READING_STRATEGY,
                        "reading_source_key": fresh["reading_source_key"], "reading_status": "completed"}}]}
            published = service.publish_matrix_enrichment(fixture.first, fixture.project_id,
                payload, built, candidate_only=True)

        rows = {row["paper_id"]: row for row in published["matrix_snapshot"]["rows"]}
        facts = rows[edited_row["paper_id"]]["scientific_facts"]
        assert next(fact for fact in facts if fact["fact_id"] == old["fact_id"]) == old
        audited = next(fact for fact in facts if fact["fact_id"] == revision["fact_id"])
        assert audited["value"] == revision["value"]
        assert audited["verification"]["status"] == "unsupported"
        assert rows[fresh_row["paper_id"]]["paper_analysis"]["usage"] == "navigation_only"
        cached = rows[cached_row["paper_id"]]
        assert cached["scientific_facts"] == cached_row["scientific_facts"]
        assert cached["paper_analysis"] == cached_row["paper_analysis"]
        assert cached["fact_enrichment"]["fact_extraction_profile"] == cached_row["fact_enrichment"]["fact_extraction_profile"]
        assert snapshot == before
    finally:
        fixture.tearDown()
