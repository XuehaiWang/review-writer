from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_writer_api.routers import sections
from review_writer_api.job_handlers.sections import SectionJobHandlers


def test_section_generation_accepts_only_the_standard_request(monkeypatch):
    submitted = []

    def submit(*args, **kwargs):
        submitted.append(kwargs)
        return kwargs["payload"]

    service = SimpleNamespace(generation_payload=lambda *a, **k: {"project_id": "project", "tasks": []})
    jobs = SimpleNamespace(repository=SimpleNamespace(get_current_job=lambda *a, **k: None), submit=submit)
    monkeypatch.setattr(sections, "_job_response", lambda job: job)
    app = FastAPI()
    app.include_router(sections.build_sections_router(lambda: SimpleNamespace(user_id="user"), service, jobs))

    with TestClient(app) as client:
        standard = client.post("/api/v1/projects/project/sections/jobs", json={})
        retired = client.post("/api/v1/projects/project/sections/jobs", json={"experimental_findings": True})

    assert standard.status_code == 202
    assert "experimental_findings" not in standard.json()
    assert retired.status_code == 422
    assert len(submitted) == 1


def test_retired_job_keeps_saved_outputs_without_starting_a_new_run():
    class Handler(SectionJobHandlers):
        def _staging(self, *args):
            raise AssertionError("must not touch saved or staging files")

    try:
        Handler().sections_generate(None, {"experimental_findings": True})
    except RuntimeError as error:
        assert "retired experimental writing job" in str(error)
    else:
        raise AssertionError("retired job must be rejected")
