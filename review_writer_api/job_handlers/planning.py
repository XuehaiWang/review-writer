"""Matrix and Planning native job handlers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from review_writer_api.errors import WorkflowError
from review_writer_api.job_handlers.support import matrix_live_payload as _matrix_live_payload
from review_writer_api.security import Principal, Role


class PlanningJobHandlers:
    def blueprint_plan(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        checkpoint_path, progress_path = staging / "blueprint-checkpoint.json", staging / "blueprint-progress.json"
        self._write_json(checkpoint_path, payload.get("blueprint_checkpoint") or {})
        settings = getattr(getattr(self, "model_gateway", None), "settings", None)
        limits = dict(payload.get("academic_planning_limits") or {})
        limits["section_concurrency"] = max(1, min(3, int(limits.get("section_concurrency", 2)),
            int(getattr(settings, "model_gateway_max_concurrency", os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_CONCURRENCY", 2))),
            int(getattr(settings, "model_gateway_user_concurrency", os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_USER_CONCURRENCY", 1)))))
        inputs = {**payload, "academic_planning_limits": limits}
        self._write_json(staging / "blueprint-input.json", inputs)
        previous = None
        def progress():
            nonlocal previous
            context.checkpoint()
            if not progress_path.is_file() or not checkpoint_path.is_file():
                return
            status = json.loads(progress_path.read_text(encoding="utf-8"))
            state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (status, state) == previous:
                return
            previous = (status, state)
            context.report_progress(int(status.get("current", 0)), int(status.get("total", 0)))
            context.report_partial_result({"blueprint_checkpoint": state, "blueprint_progress": status})
        normal, secrets = self._text_gateway_environment(context)
        body_count = sum(s.get("section_role") == "body" for s in inputs["section_blueprint"]["sections"])
        waves = (body_count + limits["section_concurrency"] - 1) // limits["section_concurrency"]
        self.runner.run(
            [sys.executable, "-m", "review_writer_core.stages.planning.academic_planning",
             "--input", str(staging / "blueprint-input.json"), "--output", str(staging / "blueprint-output.json"),
             "--checkpoint", str(checkpoint_path), "--progress", str(progress_path)],
            cwd=self.root, staging_directory=staging, expected_outputs=("blueprint-output.json",),
            env=normal, secret_env=secrets, cancel_requested=context.cancellation_requested,
            progress_callback=progress, timeout_seconds=min(8 * 3600, max(1800, (waves + 1) * 1200)),
        )
        progress()
        result = self._result(staging, "blueprint-output.json")
        result["blueprint_progress"] = json.loads(progress_path.read_text(encoding="utf-8"))
        return result

    @staticmethod
    def _matrix_progress_callback(context, status_file: Path, checkpoint_file: Path):
        """Publish live Matrix facts without exposing mutable staging paths."""

        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                checkpoint = {}
                if checkpoint_file.is_file():
                    loaded = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        checkpoint = loaded
                fingerprint = json.dumps(
                    {"status": status, "checkpoint": checkpoint},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if fingerprint == previous:
                    return
                previous = fingerprint
                current = max(0, int(status.get("current") or 0))
                total = max(0, int(status.get("total") or 0))
                if total:
                    current = min(current, total)
                if hasattr(context, "report_progress"):
                    context.report_progress(current, total)
                if hasattr(context, "report_partial_result"):
                    result = {
                        "matrix_enrichment_progress": status,
                        "matrix_enrichment_live": _matrix_live_payload(
                            status, checkpoint
                        ),
                    }
                    if checkpoint:
                        result["matrix_enrichment_checkpoint"] = checkpoint
                    context.report_partial_result(result)
            except Exception:
                # Live reporting is observational and must never fail extraction.
                return

        return callback

    def matrix_enrich(self, context, payload):
        """Extract per-paper Matrix facts through the scoped text gateway."""

        staging = self._staging(context.user_id, context.job_id)
        payload["attempt_id"] = str(context.job_id)
        settings = getattr(getattr(self, "model_gateway", None), "settings", None)
        global_slots = int(getattr(settings, "model_gateway_max_concurrency",
                                   os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_CONCURRENCY", 2)))
        user_slots = int(getattr(settings, "model_gateway_user_concurrency",
                                 os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_USER_CONCURRENCY", 1)))
        limits = dict(payload.get("fact_agent_limits") or {})
        limits["paper_concurrency"] = max(1, min(3, global_slots, user_slots,
                                                int(limits.get("paper_concurrency", 3))))
        payload["fact_agent_limits"] = limits
        input_path = staging / "matrix-enrichment-input.json"
        output_path = staging / "matrix-enrichment-output.json"
        progress_path = staging / "matrix-enrichment-progress.json"
        checkpoint_path = staging / "matrix-enrichment-checkpoint.json"
        request_path = staging / "matrix-evidence-request.json"
        response_path = staging / "matrix-evidence-response.json"
        principal = Principal(context.user_id, frozenset({Role.USER}))
        planning = getattr(self, "planning_service", None)
        resume_checkpoint = payload.get("resume_checkpoint")
        if isinstance(resume_checkpoint, dict):
            if planning is not None:
                papers = {str(paper.get("paper_id")): paper for paper in payload.get("papers") or []}
                for paper_id, entry in (resume_checkpoint.get("entries") or {}).items():
                    if entry.get("source_fingerprint") != papers.get(paper_id, {}).get("source_fingerprint"):
                        continue
                    for request in (entry.get("agent_state") or {}).get("retrieval_requests") or []:
                        for question in request.get("questions") or []:
                            context.checkpoint()
                            try:
                                planning.retrieve_matrix_fact_evidence(
                                    principal, str(context.project_id), payload, {**request, "questions": [question]}
                                )
                            except WorkflowError:
                                # Ownership/version/integrity failures are not
                                # transient retrieval outages to be bypassed.
                                raise
                            except Exception as exc:
                                state = entry.setdefault("agent_state", {})
                                state.setdefault("pending_requests", []).append(question)
                                state["resume_retrieval_error"] = type(exc).__name__
            self._write_json(checkpoint_path, resume_checkpoint)
        self._write_json(input_path, payload)
        report_progress = self._matrix_progress_callback(context, progress_path, checkpoint_path)
        answered: set[str] = set()

        def progress_callback():
            context.checkpoint()
            if planning is not None and request_path.is_file():
                request = json.loads(request_path.read_text(encoding="utf-8"))
                request_id = str(request.get("request_id") or "")
                if request_id and request_id not in answered:
                    response = {"request_id": request_id, "evidence": []}
                    try:
                        response["evidence"] = planning.retrieve_matrix_fact_evidence(
                            principal, str(context.project_id), payload, request
                        )
                    except Exception as exc:
                        response["error"] = f"Evidence retrieval unavailable: {type(exc).__name__}"
                    context.checkpoint()
                    self._write_json(response_path, response)
                    answered.add(request_id)
            report_progress()
        normal, secrets = self._text_gateway_environment(context)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-literature-matrix-outline"
                    / "scripts"
                    / "enrich_matrix_facts.py"
                ),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--progress",
                str(progress_path),
                "--checkpoint",
                str(checkpoint_path),
                *(["--evidence-request", str(request_path), "--evidence-response", str(response_path)]
                  if planning is not None else []),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("matrix-enrichment-output.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=progress_callback,
            timeout_seconds=max(
                30 * 60,
                min(
                    8 * 60 * 60,
                    int(payload.get("pending_paper_count") or 1) * 8 * 60,
                ),
            ),
        )
        result = self._result(staging, "matrix-enrichment-output.json")
        if checkpoint_path.is_file():
            result["matrix_enrichment_checkpoint"] = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
        if payload.get("targeted_evidence_requests"):
            # The registry was extended by the trusted Worker retrieval
            # adapter, not by model output. Recheck source ownership/versions.
            if planning is not None:
                planning._validate_fact_sources(principal, payload.get("papers") or [])
            result["fact_repair_sources"] = payload.get("papers") or []
        return result
