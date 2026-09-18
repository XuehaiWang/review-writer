from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.domain_services.actions.draft.publication import repaired_artifact_metadata
from review_writer_api.domain_services.discovery import _candidate_id, _selected
from review_writer_api.domain_services.final import FinalService
from review_writer_api.domain_services.planning import _planning_job_payload
from review_writer_api.domain_services.sections import _job_payload
from review_writer_api.errors import WorkflowConflict
from review_writer_api.security import Principal, Role
from review_writer_api.job_service import job_payload
from review_writer_api.routers.jobs import _job_response
from review_writer_core.stages.discovery import records
from review_writer_core.stages.sections.blueprint_builder import build_section
from review_writer_core.workflow.artifacts import (
    DRAFT_MANUSCRIPT, DRAFT_QUALITY_REPORT, DRAFT_REWRITE_OVERLAYS,
    MATRIX, SECTION_EVIDENCE_PACKAGE,
)


@pytest.mark.parametrize("status,actions", [
    ("queued", ["cancel"]), ("running", ["cancel"]), ("cancel_requested", ["cancel"]),
    ("failed", ["retry"]), ("interrupted", ["retry"]), ("cancelled", ["retry"]),
    ("succeeded", []), ("idle", []),
])
def test_all_job_views_share_fields_and_action_policy(status, actions):
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    job = SimpleNamespace(id="job", project_id="project", scope="project", job_type="sections.generate",
        status=status, result={"completed_sections": ["S01"]}, progress_current=1, progress_total=2,
        cancellation_requested=False, error_code="", error_message="", retry_of_job_id=None,
        created_at=now, updated_at=now, started_at=None, finished_at=None,
        user_id="private-owner", payload={"internal": "not-a-public-job-field"}, lease_token="private-lease")
    result = job_payload(job)
    assert _planning_job_payload is _job_payload is job_payload
    assert _job_response(job).model_dump() == result
    assert result["available_actions"] == actions
    assert result["created_at"] == now.isoformat()
    assert result["started_at"] is None
    assert not {"user_id", "payload", "lease_token"}.intersection(result)


def test_discovery_identity_and_selection_are_shared():
    assert _candidate_id is records._candidate_id
    assert _selected is records._selected
    assert _candidate_id({"paper_id": " P009 ", "doi": "unrelated"}, external=False) == "P009"
    assert _candidate_id({"doi": "10.1/example", "url": "fallback"}, external=True) == "10.1/example"
    assert not _selected({"selected_for_matrix": True, "role": "excluded"})


@pytest.mark.parametrize("logical", [DRAFT_MANUSCRIPT, DRAFT_QUALITY_REPORT, DRAFT_REWRITE_OVERLAYS, MATRIX])
def test_repair_publication_preserves_history_and_binds_new_sources(logical):
    previous = {"operation": "accept", "source_draft_artifact_id": "old-draft", "custom": "kept"}
    before = deepcopy(previous)
    published = {name: SimpleNamespace(id=f"new-{name}") for name in (
        SECTION_EVIDENCE_PACKAGE, MATRIX, DRAFT_MANUSCRIPT)}
    result = repaired_artifact_metadata(previous, logical, published)
    assert previous == before
    assert result["source_section_evidence_artifact_id"] == published[SECTION_EVIDENCE_PACKAGE].id
    assert result["source_matrix_artifact_id"] == published[MATRIX].id
    assert result["source_draft_artifact_id"] == (
        published[DRAFT_MANUSCRIPT].id if logical in {DRAFT_QUALITY_REPORT, DRAFT_REWRITE_OVERLAYS} else "old-draft")
    assert result["custom"] == "kept"
    assert repaired_artifact_metadata(previous, logical, {}) == previous


def test_legacy_final_conclusion_preserves_approval_and_manual_text_exclusion():
    method = "conclusion_payload"
    service = object.__new__(FinalService)
    principal = SimpleNamespace(user_id="owner")
    draft = SimpleNamespace(id="draft-id")
    service._approved_draft = Mock(return_value=("full text", draft, {}))
    service._revision = Mock(return_value=7)
    service.drafts = SimpleNamespace(
        automatic_synthesis_source=Mock(return_value={"draft_text": "verified text",
            "source_quality_artifact_id": "quality-id", "excluded_manual_paragraph_ids": ["p2"]}),
        compatibility_payload=Mock(return_value={"topic": "topic"}),
    )
    result = getattr(service, method)(principal, "project")
    service._approved_draft.assert_called_once_with(principal, "project")
    service.drafts.automatic_synthesis_source.assert_called_once_with(principal, "project", text="full text", draft=draft)
    assert result["draft_text"] == "verified text"
    assert result["excluded_manual_paragraph_ids"] == ["p2"]
    assert result["source_draft_artifact_id"] == "draft-id"
    assert result["expected_revision"] == 7
    service._approved_draft.side_effect = WorkflowConflict("not approved")
    with pytest.raises(WorkflowConflict):
        getattr(service, method)(principal, "project")


def test_draft_overview_uses_saved_text_without_final_approval_but_rejects_stale_sources():
    service = object.__new__(FinalService)
    principal = Principal("owner", frozenset({Role.USER}))
    draft = SimpleNamespace(id="draft-id")
    service._approved_draft = Mock(side_effect=AssertionError("Overview belongs to Draft"))
    service._revision = Mock(return_value=7)
    service.drafts = SimpleNamespace(
        _read_text=Mock(return_value=("Saved text including manual edits", draft)),
        _freshness=Mock(return_value={"upstream_stale": False}),
        compatibility_payload=Mock(return_value={"topic": "topic"}),
    )
    result = service.overview_payload(principal, "project")
    assert result["draft_text"] == "Saved text including manual edits"
    assert result["source_draft_artifact_id"] == "draft-id"
    assert result["draft_creation"] is True
    service._approved_draft.assert_not_called()
    service.drafts._freshness.return_value = {"upstream_stale": True}
    with pytest.raises(WorkflowConflict):
        service.overview_payload(principal, "project")


@pytest.mark.parametrize("assignment", [
    {"assigned_papers": []}, {"primary_papers": []}, {"supporting_papers": []},
    {"assigned_papers": ["missing-paper"]},
])
def test_explicit_blueprint_assignments_are_not_replaced_by_arbitrary_papers(assignment):
    papers = [{"paper_id": "P001", "method": "message passing", "main_finding": "predictive performance"}]
    section = {"section_id": "S02", "title": "Graph methods", **assignment}
    result = build_section(section, papers, [], {}, "", "")
    assert result["major_papers"] == []


def test_legacy_outline_without_assignment_still_uses_supported_selection():
    papers = [{"paper_id": "P001", "method": "message passing", "main_finding": "predictive performance"}]
    result = build_section({"section_id": "S02", "title": "Graph methods"}, papers, [], {}, "", "")
    assert result["major_papers"] == ["P001"]
