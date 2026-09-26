"""User-visible 03/04 handoff does not change internal approval state."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.repositories import ProjectRecord
from review_writer_api.security import Principal, Role
from review_writer_api.services import ProjectService


class ProjectRecords:
    def __init__(self, *records: ProjectRecord):
        self.records = records

    def list_for_user(self, _user_id):
        return list(self.records)

    def get_for_user(self, _user_id, project_id):
        return next((record for record in self.records if record.project_id == project_id), None)


def record(*, matrix_status="review", blueprint_status="pending"):
    return ProjectRecord(
        project_id="project-1", slug="review", owner_user_id="owner",
        topic="Topic", discovery_status="approved", completed_stages=("discovery",),
        current_stage="matrix", stage_states={
            "discovery": {"status": "approved"},
            "matrix": {"status": matrix_status},
            "blueprint": {"status": blueprint_status},
        },
    )


def test_saved_current_outline_moves_visible_stage_to_four_without_approving_matrix():
    original = record()
    service = ProjectService(ProjectRecords(original))
    service.outline_ready_for_chapter_planning = lambda _principal, _project_id: True
    principal = Principal("owner", frozenset({Role.USER}))

    item = service.get_project(principal, original.project_id)
    assert item.current_stage == "sections"
    assert "matrix" in item.completed_stages
    assert item.stage_states["matrix"]["status"] == "review"
    assert service.list_projects(principal)[0].current_stage == item.current_stage


def test_missing_or_stale_outline_keeps_stage_three():
    principal = Principal("owner", frozenset({Role.USER}))
    service = ProjectService(ProjectRecords(record()))
    service.outline_ready_for_chapter_planning = lambda _principal, _project_id: False
    assert service.get_project(principal, "project-1").current_stage == "planning"

    stale = ProjectService(ProjectRecords(record(matrix_status="stale")))
    stale.outline_ready_for_chapter_planning = lambda _principal, _project_id: False
    assert stale.get_project(principal, "project-1").current_stage == "planning"


def test_approved_internal_matrix_without_a_current_outline_stays_in_stage_three():
    principal = Principal("owner", frozenset({Role.USER}))
    original = replace(record(matrix_status="approved"), completed_stages=("discovery", "matrix"))
    service = ProjectService(ProjectRecords(original))
    service.outline_ready_for_chapter_planning = lambda _principal, _project_id: False

    visible = service.get_project(principal, "project-1")
    assert visible.current_stage == "planning"
    assert "matrix" not in visible.completed_stages
    assert visible.stage_states["matrix"]["status"] == "approved"


def test_outline_readiness_requires_complete_current_version_and_nonstale_matrix():
    principal = Principal("owner", frozenset({Role.USER}))
    service = PlanningService.__new__(PlanningService)
    matrix = {"rows": []}
    matrix_artifact = SimpleNamespace(id="matrix-2")
    outline = {"outline_complete": True, "outline_md": "## Current", "source_matrix_artifact_id": "matrix-2"}
    outline_artifact = SimpleNamespace(id="outline-1")
    state = SimpleNamespace(status="review")
    service.repository = SimpleNamespace(
        get_stage_state=lambda *_args: state,
        get_current_artifact=lambda *_args: matrix_artifact,
    )
    service._read_json = lambda *_args, **_kwargs: (outline, outline_artifact)
    service._matrix = lambda *_args: (matrix, matrix_artifact)

    assert service.outline_ready_for_chapter_planning(principal, "project-1") is True
    outline["outline_md"] = ""
    assert service.outline_ready_for_chapter_planning(principal, "project-1") is False
    outline["outline_md"] = "## Current"
    outline["source_matrix_artifact_id"] = "matrix-old"
    service._matrix_dependency_matches = lambda *_args: False
    assert service.outline_ready_for_chapter_planning(principal, "project-1") is False
    outline["source_matrix_artifact_id"] = "matrix-2"
    state.status = "stale"
    assert service.outline_ready_for_chapter_planning(principal, "project-1") is False
