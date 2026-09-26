"""Download history is a read-only, public hint, not a full-text guarantee."""
import pytest
from review_writer_api.domain_services.discovery import download_access_summary


@pytest.mark.parametrize("error,reason", [
    ("HTTPError: HTTP 429", "rate_limited"),
    ("TimeoutError: timed out", "timeout"),
    ("HTTP Error 403", "access_restricted"),
    ("HTTP Error 404", "broken_link"),
])
def test_provider_failure_has_public_action_without_raw_error(error, reason):
    assert download_access_summary("succeeded", {
        "status": "all_sources_failed", "provider_attempts": [{"error": error}],
    }) == {"state": "not_acquired", "reason": reason}


def test_oa_missing_is_not_a_paywall_assertion():
    assert download_access_summary("succeeded", {"status": "no_open_access_pdf"}) == {
        "state": "not_acquired", "reason": "not_found"}


@pytest.mark.parametrize("outcome", ["downloaded", "already_in_library", "duplicate_file"])
def test_download_does_not_claim_indexing_is_complete(outcome):
    assert download_access_summary("succeeded", {"status": outcome}) == {"state": "downloaded", "reason": ""}


def test_running_and_missing_result():
    assert download_access_summary("running", {})["state"] == "acquiring"
    assert download_access_summary("failed", {}) == {"state": "not_acquired", "reason": "failed"}


def test_history_projection_is_scoped_latest_and_does_not_change_artifact(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from types import SimpleNamespace
    from uuid import uuid4
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from review_writer_api.database import Base
    from review_writer_api.workflow_models import WorkflowJob
    from review_writer_api.domain_services import discovery as module

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    user, other, project = uuid4(), uuid4(), str(uuid4())
    now = datetime.now(timezone.utc)
    try:
        with sessions.begin() as session:
            for index, (owner, pid, outcome) in enumerate([
                (user, project, "all_sources_failed"),
                (user, project, "downloaded"),
                (other, project, "all_sources_failed"),
                (user, str(uuid4()), "all_sources_failed"),
            ]):
                session.add(WorkflowJob(user_id=owner, scope="library", job_type="library.download",
                    status="succeeded", idempotency_key=str(index), created_at=now + timedelta(seconds=index),
                    payload_json={"acquisition_project_id": pid, "candidates": [{"candidate_id": "C1"}]},
                    result_json={"results": [{"candidate_id": "C1", "status": outcome}]}))
        payload = {"results": [{"keyword": "test", "web_results": [{"candidate_id": "C1", "title": "Paper"}]}]}
        service = module.DiscoveryService.__new__(module.DiscoveryService)
        service.repository = SimpleNamespace(session_factory=sessions,
            get_stage_state=lambda *args: SimpleNamespace(revision=3, status="review"),
            get_current_artifact=lambda *args: None)
        service._read_current = lambda *args: (payload, SimpleNamespace(id="artifact"))
        monkeypatch.setattr(module, "library_paper_labels", lambda *args: {})
        result = service.get(SimpleNamespace(user_id=str(user)), project)
        assert result["results"][0]["web_results"][0]["fulltext_access"]["state"] == "downloaded"
        assert result["revision"] == 3
        assert "fulltext_access" not in payload["results"][0]["web_results"][0]
    finally:
        engine.dispose()
