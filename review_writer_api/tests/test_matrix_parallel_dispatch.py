"""Offline coverage of Matrix delegation across the real subprocess boundary."""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from collections import Counter
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from review_writer_api.native_handlers import NativeWorkflowHandlers
from review_writer_api.scientific_runner import ScientificModelDeferred, ScientificRunner
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.scientific_facts import fact_is_usable


class _DelegationServer:
    """A local durable-job cache; each new request key represents one paid call."""

    def __init__(self):
        self.lock = threading.Lock()
        self.first_wave = threading.Barrier(2)
        self.requests = {}
        self.jobs = {}
        self.ready = set()
        self.calls = Counter()
        self.paid_calls = Counter()
        self.post_calls = Counter()
        self.errors = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    assert self.path == "/api/internal/model-delegations"
                    assert self.headers["Authorization"] == "Bearer offline-test-token"
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    state.post_calls[request["stage"]] += 1
                    response = state.submit(request)
                except Exception as exc:
                    with state.lock:
                        state.errors.append(repr(exc))
                    self.send_response(400)
                    response = {"error": "Offline fixture contract failed"}
                else:
                    self.send_response(200)
                body = json.dumps(response).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                assert self.headers["Authorization"] == "Bearer offline-test-token"
                key = self.path.rsplit("/", 1)[1]
                with state.lock:
                    request = deepcopy(state.requests[key])
                body = json.dumps(state.submit(request)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def gateway_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/api/internal/model-calls"

    def submit(self, request):
        stage, key = request["stage"], request["request_key"]
        with self.lock:
            self.calls[stage] += 1
            is_new = key not in self.requests
            if is_new:
                self.requests[key] = deepcopy(request)
                self.paid_calls[stage] += 1
                self.jobs[key] = str(uuid.uuid4())
            assert self.requests[key] == request, "A replay must keep the original request"
            assert self.paid_calls[stage] == 1, "A resume created another paid key for this stage"
            model_job_id = self.jobs[key]
            is_ready = stage in self.ready
        if is_new and stage in {"matrix-facts-P1", "matrix-facts-P2"}:
            # A serial handler cannot satisfy this rendezvous. It also proves the
            # first child does not stop the second HTTP submission from occurring.
            self.first_wave.wait(timeout=8)
        if not is_ready:
            return {"status": "running", "model_job_id": model_job_id}
        if stage.startswith("matrix-facts-"):
            paper_id = stage.removeprefix("matrix-facts-")
            result = {"facts": [{
                "field_id": "quantitative_results", "value": "91% yield",
                "evidence_key": f"key-{paper_id}",
                "support_excerpt": "The optimized protocol gave 91% yield.",
                "confidence": 0.99,
            }]}
        else:
            assert stage.startswith("fact-verify-")
            candidates = request["prompt"].split("\nSource context:\n")[0]
            facts = json.loads(candidates[candidates.rindex("\n[") + 1:])
            result = {"verdicts": [{
                "fact_id": fact["fact_id"], "status": "supported",
                "reason": "The same experiment explicitly reports this result.",
            } for fact in facts]}
        return {"status": "succeeded", "model_job_id": model_job_id,
                "output_text": json.dumps(result)}

    def finish(self, stage):
        with self.lock:
            assert stage in self.calls, "Only an already submitted job can finish"
            self.ready.add(stage)

    def job_id(self, stage):
        with self.lock:
            return next(self.jobs[key] for key, request in self.requests.items()
                        if request["stage"] == stage)


def _paper(paper_id):
    return {
        "paper_id": paper_id, "source_fingerprint": f"source-{paper_id}",
        "required_fact_roles": ["quantitative_results"],
        "evidence_candidates": [{
            "evidence_key": f"key-{paper_id}", "chunk_id": "result",
            "source_lineage_hash": f"lineage-{paper_id}",
            "question_ids": ["quantitative_results"], "content_type": "text",
            "content": "The optimized protocol gave 91% yield.",
        }],
    }


@pytest.fixture
def isolated_workspace():
    with tempfile.TemporaryDirectory(prefix="matrix-dispatch-test-") as directory:
        yield Path(directory)


@pytest.mark.parametrize("review_reading", [False, True])
def test_native_matrix_parallel_dispatch_and_resume_through_real_subprocess(isolated_workspace, monkeypatch, review_reading):
    monkeypatch.setenv("REVIEW_WRITER_DELEGATED_MODEL_ENABLED", "1")
    progress, partial_results = [], []
    context = SimpleNamespace(
        user_id=str(uuid.uuid4()), project_id="offline-matrix", job_id=str(uuid.uuid4()),
        job_type="matrix.enrich", checkpoint=lambda: None,
        cancellation_requested=lambda: False,
        report_progress=lambda current, total: progress.append((current, total)),
        report_partial_result=lambda value: partial_results.append(deepcopy(value)),
    )
    source = {
        "project_id": context.project_id, "source_matrix_artifact_id": "matrix-fixture",
        "pending_paper_count": 3, "papers": [_paper(f"P{i}") for i in (1, 2, 3)],
        "fact_agent_limits": {"paper_concurrency": 3, "max_model_calls": 2,
                              "max_supplement_rounds": 0},
    }
    if review_reading:
        source["reading_strategy"] = "review-oriented/1"
        for paper in source["papers"]:
            paper["reading_strategy"] = "review-oriented/1"
            paper["reading_source_key"] = paper["source_fingerprint"]
            paper["evidence_candidates"] += [
                {**paper["evidence_candidates"][0], "evidence_key": f"{paper['paper_id']}-window-{index}",
                 "content": f"Additional evidence window {index}."} for index in range(8)]
    # Each local child finishes in under a poll interval; the runner still
    # publishes its terminal checkpoint, including its exit-75 wait snapshot.
    runner = ScientificRunner(max_attempts=1, poll_interval=1)
    with _DelegationServer() as gateway:
        def get_job(user_id, job_id):
            assert user_id == context.user_id
            with gateway.lock:
                key = next(key for key, value in gateway.jobs.items() if value == job_id)
                request = deepcopy(gateway.requests[key])
            return SimpleNamespace(
                id=job_id, user_id=user_id, project_id=context.project_id, job_type="model.dispatch",
                payload={**request, "parent_job_id": context.job_id},
            )
        context.repository = SimpleNamespace(get_job=get_job)
        gateway_adapter = SimpleNamespace(
            settings=SimpleNamespace(model_gateway_max_concurrency=4,
                                     model_gateway_user_concurrency=2),
            environment_for_job=lambda _: (
                {"REVIEW_WRITER_MODEL_GATEWAY_URL": gateway.gateway_url},
                {"REVIEW_WRITER_TASK_TOKEN": "offline-test-token"},
            ),
        )
        handler = NativeWorkflowHandlers(
            runner, HostedWorkspaceManager(isolated_workspace / "workspaces"), None,
            model_gateway=gateway_adapter,
        )
        staging = handler._staging(context.user_id, context.job_id)
        checkpoint_path = staging / "matrix-enrichment-checkpoint.json"

        def checkpoint():
            return json.loads(checkpoint_path.read_text(encoding="utf-8"))

        def run():
            for paper in source["papers"]:
                paper["evidence_candidates"].reverse()
            payload = deepcopy(source)
            if partial_results:
                # Use the handler's published checkpoint, as a persisted worker
                # resume would; the child must not rely only on in-memory state.
                payload["resume_checkpoint"] = deepcopy(
                    partial_results[-1]["matrix_enrichment_checkpoint"])
            with patch.object(runner, "run", wraps=runner.run) as actual_run:
                try:
                    return handler.matrix_enrich(context, payload)
                finally:
                    command = actual_run.call_args.args[0]
                    assert Path(command[1]).name == "enrich_matrix_facts.py"
                    assert command[command.index("--max-new-papers") + 1] == "2"
                    saved = json.loads((staging / "matrix-enrichment-input.json").read_text(encoding="utf-8"))
                    assert saved["fact_agent_limits"] == {
                        "paper_concurrency": 2, "max_model_calls": 2,
                        "max_supplement_rounds": 0,
                    }
                    assert not gateway.errors, gateway.errors

        def expect_waiting(*stages):
            with pytest.raises(ScientificModelDeferred) as waiting:
                run()
            assert set(stages).issubset(gateway.calls), (dict(gateway.calls), checkpoint())
            ids = [gateway.job_id(stage) for stage in stages]
            assert waiting.value.model_job_id == ids[0]
            assert waiting.value.model_job_ids == ids
            state = checkpoint()
            assert partial_results[-1]["matrix_enrichment_checkpoint"] == state
            return state

        state = expect_waiting("matrix-facts-P1", "matrix-facts-P2")
        assert set(gateway.calls) == {"matrix-facts-P1", "matrix-facts-P2"}
        assert set(state["entries"]) == {"P1", "P2"}
        assert state["completed_papers"] == []
        assert all(entry["agent_state"]["model_calls"] == 0
                   and entry["agent_state"]["attempt_model_calls"] == 0
                   for entry in state["entries"].values())

        if review_reading:
            gateway.finish("matrix-facts-P2")
            state = expect_waiting("matrix-facts-P1", "matrix-facts-P3")
            assert state["completed_papers"] == ["P2"]
            gateway.finish("matrix-facts-P3")
            expect_waiting("matrix-facts-P1")
            gateway.finish("matrix-facts-P1")
            result = run()
            assert all(paper["processing"]["reading"] == "completed" for paper in result["papers"])
            assert all(paper["fact_extraction_profile"]["model_calls"] == 1 for paper in result["papers"])
            assert all(paper["paper_analysis"]["reading_scope"]["selected_count"] == 9 for paper in result["papers"])
            assert gateway.paid_calls == gateway.post_calls == Counter({f"matrix-facts-P{i}": 1 for i in (1, 2, 3)})
            completed_calls = gateway.calls.copy()
            assert run()["papers"] == result["papers"]
            assert gateway.calls == completed_calls
            return

        gateway.finish("matrix-facts-P2")
        state = expect_waiting("matrix-facts-P1", "fact-verify-P2")
        assert state["entries"]["P2"]["result"]["facts"]
        assert "matrix-facts-P3" not in gateway.calls
        gateway.finish("fact-verify-P2")
        state = expect_waiting("matrix-facts-P1", "matrix-facts-P3")
        assert state["completed_papers"] == ["P2"]
        retained_p2 = deepcopy(state["entries"]["P2"])
        assert gateway.calls["matrix-facts-P2"] == gateway.calls["fact-verify-P2"] == 2

        gateway.finish("matrix-facts-P3")
        expect_waiting("matrix-facts-P1", "fact-verify-P3")
        gateway.finish("fact-verify-P3")
        state = expect_waiting("matrix-facts-P1")
        assert set(state["completed_papers"]) == {"P2", "P3"}
        assert state["entries"]["P2"] == retained_p2
        gateway.finish("matrix-facts-P1")
        expect_waiting("fact-verify-P1")
        gateway.finish("fact-verify-P1")
        result = run()

        assert [paper["paper_id"] for paper in result["papers"]] == ["P1", "P2", "P3"]
        assert all(fact_is_usable(paper["facts"][0]) for paper in result["papers"])
        assert all(paper["fact_extraction_profile"]["model_calls"] == 2
                   for paper in result["papers"])
        assert gateway.paid_calls == Counter({f"{stage}-P{i}": 1
                                             for stage in ("matrix-facts", "fact-verify")
                                             for i in (1, 2, 3)})
        assert gateway.post_calls == gateway.paid_calls
        assert gateway.calls["matrix-facts-P2"] == gateway.calls["fact-verify-P2"] == 2
        assert progress[-1] == (3, 3)
        assert max(len(part["matrix_enrichment_progress"]["active_paper_ids"])
                   for part in partial_results) <= 2
        completed_calls = gateway.calls.copy()
        assert run()["papers"] == result["papers"]
        assert gateway.calls == completed_calls
