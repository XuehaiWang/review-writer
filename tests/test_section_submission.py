from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_writer_api.errors import WorkflowConflict
from review_writer_api.routers.sections import build_sections_router


@pytest.mark.parametrize("race", [False, True])
def test_duplicate_section_submit_rejoins_active_job(race):
    active = SimpleNamespace(id="existing", status="running")
    service = Mock()
    service.generation_payload.return_value = {"evidence_preparation_pending": True}
    jobs = Mock()
    jobs.repository.get_current_job.side_effect = [None, active] if race else [active]
    jobs.submit.side_effect = WorkflowConflict("Already active", details={"current_job_id": "existing"})
    app = FastAPI()
    app.include_router(build_sections_router(lambda: SimpleNamespace(user_id="owner"), service, jobs))
    with patch("review_writer_api.routers.sections._job_response", side_effect=lambda job: {"id": job.id}):
        with TestClient(app) as client:
            response = client.post("/api/v1/projects/project/sections/jobs", json={})
    assert response.status_code == 202
    assert response.json() == {"id": "existing"}
    assert jobs.submit.call_count == int(race)
    assert service.generation_payload.call_args.kwargs == {"defer_evidence": True}
    assert jobs.repository.get_current_job.call_args.kwargs["project_id"] == "project"


def test_section_submit_enqueues_lightweight_snapshot():
    service, jobs = Mock(), Mock()
    service.generation_payload.return_value = {"evidence_preparation_pending": True, "tasks": []}
    jobs.repository.get_current_job.return_value = None
    jobs.submit.return_value = SimpleNamespace(id="new")
    app = FastAPI()
    app.include_router(build_sections_router(lambda: SimpleNamespace(user_id="owner"), service, jobs))
    with patch("review_writer_api.routers.sections._job_response", side_effect=lambda job: {"id": job.id}):
        with TestClient(app) as client:
            response = client.post("/api/v1/projects/project/sections/jobs", json={}, headers={"Idempotency-Key": "stable"})
    assert response.status_code == 202
    assert jobs.submit.call_args.kwargs["payload"]["evidence_preparation_pending"] is True
    assert jobs.submit.call_args.kwargs["idempotency_key"] == "stable"
