"""Regression coverage for queued inputs, publication and prompt completeness."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_api.domain_services.base import ArtifactBackedService
from review_writer_api.errors import WorkflowConflict
from review_writer_api.job_handlers.lifecycle import register_publishing_handler
from review_writer_core.claim_contracts import claim_planning_prompt_block
from review_writer_core.draft_quality import (
    full_draft_quality_provenance, quality_input_artifact_ids,
    reusable_full_draft_quality,
)
from review_writer_core.workflow.artifacts import DRAFT_QUALITY_REPORT


def test_explicitly_missing_artifact_is_a_snapshot_dependency():
    service = ArtifactBackedService()
    service._owned_project = Mock()
    service.repository = Mock()
    service.repository.get_current_artifact.return_value = None
    principal = SimpleNamespace(user_id="owner")
    payload = {"source_quality_artifact_id": ""}
    fields = {"source_quality_artifact_id": DRAFT_QUALITY_REPORT}
    assert service.validate_artifact_inputs(principal, "project", payload, fields) == {
        DRAFT_QUALITY_REPORT: "",
    }
    service.repository.get_current_artifact.return_value = SimpleNamespace(id="new-quality")
    with pytest.raises(WorkflowConflict) as error:
        service.validate_artifact_inputs(principal, "project", payload, fields)
    assert error.value.details["field"] == "source_quality_artifact_id"


@pytest.mark.parametrize("field", ["source_blueprint_artifact_id", "source_figure_manifest_artifact_id"])
def test_quality_reuse_tracks_every_consumed_planning_and_figure_version(field):
    payload = {field: "version-1", "source_matrix_artifact_id": "matrix-1"}
    quality = {"source_draft_artifact_id": "draft-1", **full_draft_quality_provenance(
        "unchanged draft", [], input_artifact_ids=quality_input_artifact_ids(payload),
    )}
    payload[field] = "version-2"
    reusable, reasons = reusable_full_draft_quality(
        quality, draft_artifact_id="draft-1", draft_text="unchanged draft",
        paragraphs=[], input_artifact_ids=quality_input_artifact_ids(payload),
    )
    assert not reusable
    assert "input_artifacts_changed" in reasons


def _registered_handler(builder, publisher, validate=None):
    jobs = Mock()
    register_publishing_handler(jobs, "draft.evaluate", builder, publisher,
                               progress_total=3, validate=validate)
    return jobs.register_handler.call_args.args[1]


def test_stale_queued_input_never_calls_provider():
    builder = Mock()
    publisher = Mock()
    validate = Mock(side_effect=WorkflowConflict("changed"))
    handler = _registered_handler(builder, publisher, validate)
    context = SimpleNamespace(user_id="owner", project_id="project", report_progress=Mock())
    with pytest.raises(WorkflowConflict):
        handler(context, {})
    builder.assert_not_called()
    publisher.assert_not_called()


def test_cancellation_after_publication_does_not_misreport_committed_job():
    committed = False
    def checkpoint():
        if committed:
            raise RuntimeError("late cancellation")
    def publish(*_args):
        nonlocal committed
        committed = True
        return {"artifact_id": "published"}
    context = SimpleNamespace(
        user_id="owner", project_id="project", job_id="job", lease_token="lease",
        lease_generation=7, repository=Mock(), checkpoint=checkpoint,
        report_progress=lambda *_args: checkpoint(),
    )
    result = _registered_handler(Mock(return_value={}), publish)(context, {})
    assert result == {"artifact_id": "published"}
    context.repository.update_job_progress.assert_called_once_with(
        "job", 3, 3, lease_token="lease", lease_generation=7,
    )


def test_large_claim_contract_is_complete_json_and_keeps_final_fact_binding():
    claims = [{"claim_id": f"C{i}", "proposition": "Evidence-limited statement " * 30,
               "fact_ids": [f"F{i}"], "evidence_refs": [{"evidence_key": f"E{i}"}],
               "assertion_ceiling": "author_interpretation"} for i in range(42)]
    states = [{"claim_id": "C0", "status": "evidence_supported"},
              {"claim_id": "C-missing", "status": "evidence_missing", "missing_fact_ids": ["F-missing"]}]
    block = claim_planning_prompt_block(claims, states, [{"text": "Compare conditions."}])
    encoded = [line.split(": ", 1)[1] for line in block.splitlines()]
    assert len(encoded[0]) > 10000
    assert json.loads(encoded[0]) == claims
    assert json.loads(encoded[1]) == [states[1]]
    assert json.loads(encoded[2]) == [{"text": "Compare conditions."}]
