from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.domain_services.actions.draft.section_versions import SectionVersionsMixin
from review_writer_api.errors import WorkflowNotFound
from review_writer_core.workflow.artifacts import DRAFT_INITIAL_MANUSCRIPT, DRAFT_MANUSCRIPT


def version(identifier, source, **metadata):
    return SimpleNamespace(id=identifier, metadata={"source_sections_artifact_id": source, **metadata})


def service(current_source, initial=(), historical=()):
    value = SectionVersionsMixin()
    value._artifact = Mock(return_value=version("current", current_source))
    value.repository = Mock()
    value.repository.list_artifacts.side_effect = lambda user, project, logical: list(initial if logical == DRAFT_INITIAL_MANUSCRIPT else historical)
    return value


def test_baseline_is_the_first_snapshot_of_the_current_section_generation():
    old, first, duplicate = version("old", "previous"), version("first", "current"), version("duplicate", "current")
    api = service("current", [duplicate, first, old])
    assert api._initial_section_artifact(SimpleNamespace(user_id="u"), "p").id == "first"
    api._artifact.return_value = version("regenerated", "next")
    assert api._initial_section_artifact(SimpleNamespace(user_id="u"), "p") is None


def test_legacy_baseline_never_uses_a_modified_or_overlay_replayed_version():
    user = SimpleNamespace(user_id="u")
    modified = version("modified", "source", operation="dialogue-accept")
    overlaid = version("overlaid", "source", operation="assemble", source_rewrite_overlay_artifact_id="overlay")
    original = version("original", "source", operation="assemble")
    assert service("source", historical=[modified, overlaid, original])._initial_section_artifact(user, "p").id == "original"
    assert service("source", historical=[modified, overlaid])._initial_section_artifact(user, "p") is None


def test_history_endpoint_cannot_read_another_project_or_an_unrelated_artifact():
    api = SectionVersionsMixin()
    api.artifacts = Mock()
    for project, logical in [("other", DRAFT_MANUSCRIPT), ("p", "private.json")]:
        api.artifacts.resolve_owned_artifact.return_value = SimpleNamespace(
            artifact=SimpleNamespace(project_id=project, logical_name=logical))
        with pytest.raises(WorkflowNotFound):
            api._section_version(SimpleNamespace(user_id="u"), "p", "S1", "artifact")
