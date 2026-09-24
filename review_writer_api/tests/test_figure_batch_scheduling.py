from __future__ import annotations

from types import SimpleNamespace

import pytest

from review_writer_api.job_handlers.figures_execution import register_figure_handlers
from review_writer_api.job_service import JobYieldRequested


class _Repository:
    def __init__(self):
        self.result: dict = {}
        self.waiting = True

    def get_job(self, _user_id, _job_id):
        return SimpleNamespace(result=self.result)

    def has_queued_job(self, queue_name):
        assert queue_name == "image"
        return self.waiting

    def update_job_progress(self, _job_id, current, total, **_lease):
        self.context.progress = (current, total)


class _Context:
    user_id = "user-a"
    project_id = "project-a"
    job_id = "batch-a"
    lease_token = "lease-a"
    lease_generation = 1

    def __init__(self, repository):
        self.repository = repository
        repository.context = self
        self.progress = (0, 0)

    def checkpoint(self):
        return None

    def report_progress(self, current, total):
        self.progress = (current, total)

    def report_partial_result(self, result):
        self.repository.result = result


class _Figures:
    def resolve_redraw_item(self, _principal, _project_id, _payload, figure_id):
        return {"figure_id": figure_id}

    def publish_redraw(self, _principal, _project_id, _payload, built):
        return built


class _Jobs:
    def register_handler(self, _job_type, handler):
        self.handler = handler


def test_batch_yields_after_one_image_and_resumes_without_regenerating_it():
    repository = _Repository()
    context = _Context(repository)
    jobs = _Jobs()
    built: list[str] = []

    def builder(_context, item):
        built.append(item["figure_id"])
        return item

    register_figure_handlers(_Figures(), jobs, {"figures.redraw": builder})
    payload = {"figure_ids": ["A", "B"]}
    with pytest.raises(JobYieldRequested):
        jobs.handler(context, payload)
    assert built == ["A"]
    assert context.progress == (1, 2)
    assert [row["figure_id"] for row in repository.result["outputs"]] == ["A"]

    repository.waiting = False
    result = jobs.handler(context, payload)
    assert built == ["A", "B"]
    assert context.progress == (2, 2)
    assert [row["figure_id"] for row in result["outputs"]] == ["A", "B"]
