from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.domain_services.base import ArtifactBackedService
from review_writer_api.domain_services.discovery import DiscoveryService
from review_writer_api.domain_services.planning import PlanningService
from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.domain_services.figures import FiguresService, PAPER_CANDIDATES
from review_writer_api.domain_services.drafts import DraftsService
from review_writer_api.errors import ArtifactFileMissing, WorkflowStageNotReady, WorkflowNotFound
from review_writer_core.workflow.artifacts import BLUEPRINT, MATRIX, DISCOVERY_REVIEW, SECTION_DRAFTS


def service_with(existing):
    service = ArtifactBackedService()
    service.repository = Mock()
    service._owned_project = Mock()
    service.repository.get_current_artifact.side_effect = lambda user, project, name: existing.get(name)
    service.repository.list_project_jobs.return_value = []
    return service


def test_missing_output_points_to_first_unfinished_prerequisite():
    service = service_with({})
    with pytest.raises(WorkflowStageNotReady) as error:
        service._read_text(SimpleNamespace(user_id="u"), "p", BLUEPRINT)
    assert error.value.details["next_stage"] == "discovery"
    assert error.value.payload()["error"]["code"] == "WORKFLOW_STAGE_NOT_READY"


@pytest.mark.parametrize("service_type,reader,artifact", [
    (DiscoveryService, "_read_current", DISCOVERY_REVIEW),
    (PlanningService, "_read_json", MATRIX),
])
def test_first_use_readers_share_the_empty_state_contract(service_type, reader, artifact):
    service = service_type.__new__(service_type)
    service.repository = service_with({}).repository
    service._owned_project = Mock()
    with pytest.raises(WorkflowStageNotReady) as error:
        getattr(service, reader)(SimpleNamespace(user_id="u"), "p", artifact)
    assert error.value.details["next_stage"] == "discovery"
    service._owned_project.assert_called_once()


def test_local_assembly_does_not_require_a_model_but_writing_does():
    from review_writer_api.model_gateway import TEXT_GATEWAY_JOB_TYPES
    assert "final.build" not in TEXT_GATEWAY_JOB_TYPES
    assert "sections.generate" in TEXT_GATEWAY_JOB_TYPES


def test_missing_project_is_not_disguised_as_an_empty_stage():
    service = DiscoveryService.__new__(DiscoveryService)
    service.repository = Mock()
    service._owned_project = Mock(side_effect=WorkflowNotFound("Project not found."))
    with pytest.raises(WorkflowNotFound):
        service._read_current(SimpleNamespace(user_id="u"), "missing", DISCOVERY_REVIEW)
    service.repository.get_current_artifact.assert_not_called()


def test_running_planning_job_is_visible_without_starting_work():
    service = service_with({DISCOVERY_REVIEW: object(), MATRIX: object()})
    service.repository.list_project_jobs.return_value = [SimpleNamespace(
        job_type="planning.blueprint", status="running", progress_current=2, progress_total=5)]
    error = service._stage_not_ready(SimpleNamespace(user_id="u"), "p", BLUEPRINT)
    assert error.details["next_stage"] == "sections"
    assert error.details["active_job"]["current"] == 2


def test_registered_missing_file_is_still_an_operational_error():
    service = service_with({BLUEPRINT: SimpleNamespace(id="artifact")})
    service.artifacts = Mock()
    service.artifacts.resolve_owned_artifact.side_effect = ArtifactFileMissing("File missing")
    with pytest.raises(ArtifactFileMissing):
        service._read_text(SimpleNamespace(user_id="u"), "p", BLUEPRINT)
    service.repository.list_project_jobs.assert_not_called()


def test_sections_reader_uses_the_same_not_ready_contract():
    service = service_with({DISCOVERY_REVIEW: object(), MATRIX: object()})
    sections = SectionsService(service.repository, Mock())
    principal = SimpleNamespace(user_id="u", require=Mock())
    with pytest.raises(WorkflowStageNotReady) as error:
        sections._read_json_artifact(principal, "p", BLUEPRINT)
    assert error.value.details["next_stage"] == "sections"


@pytest.mark.parametrize("entrypoint", ["get_review", "get"])
def test_figure_pages_use_the_shared_prerequisite_guide(entrypoint):
    service = service_with({DISCOVERY_REVIEW: object(), MATRIX: object(), BLUEPRINT: object()})
    figures = FiguresService(service.repository, Mock())
    principal = SimpleNamespace(user_id="u", require=Mock())
    with pytest.raises(WorkflowStageNotReady) as error:
        getattr(figures, entrypoint)(principal, "p")
    assert error.value.details["next_stage"] == "sections"


def test_missing_registered_figure_candidate_file_remains_an_error():
    service = service_with({PAPER_CANDIDATES: SimpleNamespace(id="artifact")})
    artifacts = Mock()
    artifacts.resolve_owned_artifact.side_effect = ArtifactFileMissing("Missing candidate file")
    figures = FiguresService(service.repository, artifacts)
    with pytest.raises(ArtifactFileMissing):
        figures.get_review(SimpleNamespace(user_id="u", require=Mock()), "p")


@pytest.mark.parametrize("state", [None, SimpleNamespace(status="review")])
def test_draft_requires_figure_confirmation_with_actionable_guidance(state):
    service = service_with({name: object() for name in [DISCOVERY_REVIEW, MATRIX, BLUEPRINT, SECTION_DRAFTS]})
    service.repository.get_stage_state.return_value = state
    with pytest.raises(WorkflowStageNotReady) as error:
        DraftsService._require_figure_approval(service, SimpleNamespace(user_id="u"), "p")
    assert error.value.details["next_stage"] == "images"
    assert error.value.details["reason"] == "figure_approval_required"


def test_approved_figures_can_proceed_without_new_work():
    service = service_with({})
    service.repository.get_stage_state.return_value = SimpleNamespace(status="approved")
    DraftsService._require_figure_approval(service, SimpleNamespace(user_id="u"), "p")
    service.repository.list_project_jobs.assert_not_called()
