"""Draft evaluation, optimization, and rewrite native job handlers."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from copy import deepcopy

from review_writer_api.errors import WorkflowValidationError
from review_writer_core.writing_contracts import (
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    DRAFT_PASS_THRESHOLD,
    PARAGRAPH_PASS_THRESHOLD,
)


EVALUATE_DRAFT_TIMEOUT_SECONDS = 30 * 60
OPTIMIZE_DRAFT_MIN_TIMEOUT_SECONDS = 2 * 60 * 60
OPTIMIZE_DRAFT_TIMEOUT_PER_ITERATION_SECONDS = 45 * 60
OPTIMIZE_DRAFT_MAX_TIMEOUT_SECONDS = 6 * 60 * 60


class DraftJobHandlers:
    @staticmethod
    def _restore_artifact_urls(markdown: str, paths: dict[str, Any]) -> str:
        restored = str(markdown or "")
        for artifact_id, raw_path in (paths or {}).items():
            path = str(raw_path or "")
            if path:
                restored = restored.replace(
                    path, f"/api/v1/artifacts/{artifact_id}/content"
                )
        return restored

    @staticmethod
    def _feedback_progress_callback(context, status_file: Path, *, optimize: bool):
        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                fingerprint = json.dumps(status, ensure_ascii=False, sort_keys=True)
                if fingerprint == previous:
                    return
                previous = fingerprint
                if hasattr(context, "report_partial_result"):
                    context.report_partial_result({"feedback_status": status})
                if hasattr(context, "report_progress"):
                    phase = str(status.get("phase") or "")
                    if optimize:
                        current = {
                            "preflight": 1,
                            "source_checking": 2,
                            "scoring": 2,
                            "baseline_reused": 2,
                            "baseline_refreshing": 2,
                            "evaluated": 3,
                            "rewriting": 3,
                            "scoring_changed_paragraphs": 3,
                            "changed_paragraphs_evaluated": 3,
                            "plateau": 4,
                            "rewrite_blocked": 4,
                            "iteration_limit": 4,
                            "released": 4,
                            "validating_full_draft": 4,
                        }.get(phase, 1)
                        context.report_progress(current, 5)
                    else:
                        current = 2 if phase in {"scoring", "evaluated", "released"} else 1
                        context.report_progress(current, 3)
            except Exception:
                # Progress reporting is observational and must never invalidate
                # a scientifically valid result that is ready to publish.
                return

        return callback

    def _draft_feedback(self, context, payload, *, evaluate_only: bool):
        fact_repair = {}
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="draft-workspace"
        )
        project_id = str(payload["project_id"])
        relative_first = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
        )
        # Stage 6 children receive only a scoped task token and gateway URL.
        normal, secrets = self._text_gateway_environment(context)
        first = project / "04_first_draft"
        reference_repair = json.loads(
            (first / "reference_repair.json").read_text(encoding="utf-8")
        )
        reuse_baseline = bool(payload.get("quality_reused")) and not bool(
            reference_repair.get("changed")
        )
        if bool(payload.get("quality_reused")) and not reuse_baseline:
            payload["run_mode"] = "baseline_expired"
        command = [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "feedback_loop.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--goal",
                str(float(payload.get("goal") or DRAFT_PASS_THRESHOLD)),
                "--paragraph-goal",
                str(float(payload.get("paragraph_goal") or PARAGRAPH_PASS_THRESHOLD)),
                "--max-iterations",
                str(int(payload.get("max_iterations") or 2)),
                "--min-case-words",
                str(int(payload.get("min_case_words") or CASE_PARAGRAPH_MIN_WORDS)),
                "--max-case-words",
                str(int(payload.get("max_case_words") or CASE_PARAGRAPH_MAX_WORDS)),
            ]
        if evaluate_only:
            command.append("--evaluate-only")
        elif any(issue.get("repair_class") in {"planning_adjustment", "draft_revision"}
                 for issue in payload.get("issues") or []):
            (first / "local_revision_issues.json").write_text(
                json.dumps(payload.get("issues") or [], ensure_ascii=False), encoding="utf-8")
            command.append("--local-revision")
        if reuse_baseline:
            command.append("--reuse-baseline")
        max_iterations = int(payload.get("max_iterations") or 2)
        timeout_seconds = (
            EVALUATE_DRAFT_TIMEOUT_SECONDS
            if evaluate_only
            else min(
                OPTIMIZE_DRAFT_MAX_TIMEOUT_SECONDS,
                max(
                    OPTIMIZE_DRAFT_MIN_TIMEOUT_SECONDS,
                    max_iterations * OPTIMIZE_DRAFT_TIMEOUT_PER_ITERATION_SECONDS,
                ),
            )
        )
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_first / "rubric_evaluation.json").as_posix(),
                (relative_first / "reviewer_findings.json").as_posix(),
                (relative_first / "first_draft_gate_status.json").as_posix(),
                (relative_first / "first_draft_preflight.json").as_posix(),
                (relative_first / "original_source_check.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=self._feedback_progress_callback(
                context,
                first / "feedback_loop_status.json",
                optimize=not evaluate_only,
            ),
            timeout_seconds=timeout_seconds,
        )
        # The optimizer retains the best safe candidate per paragraph across
        # iterations.  Their combined manuscript is a new composition, so the
        # incremental candidate score is not authoritative.  Evaluate those
        # exact bytes once as a complete draft before the API can auto-apply
        # the proposal.
        batch_review_file = first / "batch_review_candidates.json"
        if not evaluate_only and batch_review_file.is_file():
            batch_review = json.loads(
                batch_review_file.read_text(encoding="utf-8")
            )
            review_changes = (
                list(batch_review.get("changes") or [])
                if isinstance(batch_review, dict)
                else []
            )
            review_candidate = (
                str(batch_review.get("candidate_draft_text") or "")
                if isinstance(batch_review, dict)
                else ""
            )
            if review_changes and review_candidate.strip():
                workspace_candidate_matches = (
                    (first / "first_draft.md").read_text(encoding="utf-8").rstrip()
                    == review_candidate.rstrip()
                )
                exact_evaluation = (
                    dict(batch_review.get("full_draft_evaluation") or {})
                    if bool(batch_review.get("full_draft_evaluated"))
                    and workspace_candidate_matches
                    else {}
                )
                if not exact_evaluation:
                    status_path = first / "feedback_loop_status.json"
                    saved_status = (
                        status_path.read_bytes() if status_path.is_file() else b""
                    )
                    (first / "first_draft.md").write_text(
                        review_candidate.rstrip() + "\n", encoding="utf-8"
                    )
                    if hasattr(context, "report_partial_result"):
                        context.report_partial_result(
                            {
                                "feedback_status": {
                                    "phase": "validating_full_draft",
                                    "review_candidate_count": len(review_changes),
                                }
                            }
                        )
                    self.runner.run(
                        [*command, "--evaluate-only"],
                        cwd=self.root,
                        staging_directory=staging,
                        expected_outputs=(
                            (relative_first / "rubric_evaluation.json").as_posix(),
                            (relative_first / "reviewer_findings.json").as_posix(),
                            (relative_first / "first_draft_gate_status.json").as_posix(),
                            (relative_first / "first_draft_preflight.json").as_posix(),
                            (relative_first / "original_source_check.json").as_posix(),
                        ),
                        env=normal,
                        secret_env=secrets,
                        cancel_requested=context.cancellation_requested,
                        progress_callback=self._feedback_progress_callback(
                            context,
                            status_path,
                            optimize=True,
                        ),
                        timeout_seconds=EVALUATE_DRAFT_TIMEOUT_SECONDS,
                    )
                    exact_evaluation = json.loads(
                        (first / "rubric_evaluation.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    if saved_status:
                        status_path.write_bytes(saved_status)
                batch_review["candidate_score"] = round(
                    float(exact_evaluation.get("total_score") or 0), 2
                )
                batch_review["full_draft_evaluated"] = True
                batch_review["full_draft_evaluation"] = exact_evaluation
                batch_review["full_draft_evaluated_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                self._write_json(batch_review_file, batch_review)
        evaluation = json.loads(
            (first / "rubric_evaluation.json").read_text(encoding="utf-8")
        )
        findings = json.loads(
            (first / "reviewer_findings.json").read_text(encoding="utf-8")
        )
        gate = json.loads(
            (first / "first_draft_gate_status.json").read_text(encoding="utf-8")
        )
        preflight = json.loads(
            (first / "first_draft_preflight.json").read_text(encoding="utf-8")
        )
        source_check = json.loads(
            (first / "original_source_check.json").read_text(encoding="utf-8")
        )
        paragraph_scores = {
            str(item.get("paragraph_id") or ""): item
            for item in evaluation.get("paragraph_scores") or []
            if isinstance(item, dict)
        }
        issues = []
        for index, finding in enumerate(findings if isinstance(findings, list) else [], 1):
            if not isinstance(finding, dict):
                continue
            paragraph_id = str(finding.get("paragraph_id") or "")
            paragraph_score = paragraph_scores.get(paragraph_id, {})
            issues.append(
                {
                    **finding,
                    "score": paragraph_score.get("score"),
                    "route": str(
                        paragraph_score.get("route") or finding.get("route") or ""
                    ),
                    "failed_dimensions": list(
                        paragraph_score.get("failed_dimensions") or []
                    ),
                    "issue_id": str(finding.get("id") or f"PAR-{index:03d}"),
                    "message": str(
                        finding.get("diagnosis")
                        or finding.get("recommended_direction")
                        or "Review this paragraph."
                    ),
                }
            )
        status_file = first / "feedback_loop_status.json"
        feedback_status = (
            json.loads(status_file.read_text(encoding="utf-8"))
            if status_file.is_file()
            else {}
        )
        quality_reused = bool(feedback_status.get("quality_reused"))
        run_mode = "steady_state" if quality_reused else str(
            feedback_status.get("run_mode") or payload.get("run_mode") or "migration"
        )
        evidence_rescue_outcomes = dict(
            (payload.get("prior_quality_context") or {}).get(
                "evidence_rescue_outcomes"
            )
            or {}
        )
        evidence_rescue_status_counts: dict[str, int] = {}
        for outcome in evidence_rescue_outcomes.values():
            if not isinstance(outcome, dict):
                continue
            outcome_status = str(outcome.get("status") or "unresolved")
            evidence_rescue_status_counts[outcome_status] = (
                evidence_rescue_status_counts.get(outcome_status, 0) + 1
            )
        feedback_status["quality_reused"] = quality_reused
        feedback_status["evidence_rescue_cache"] = dict((payload.get("prior_quality_context") or {}).get("evidence_rescue_cache") or {})
        feedback_status["run_mode"] = run_mode
        feedback_status["evidence_rescue_outcomes"] = evidence_rescue_outcomes
        feedback_status[
            "evidence_rescue_status_counts"
        ] = evidence_rescue_status_counts
        feedback_status["provider_deferred_count"] = int(
            feedback_status.get("rewrite_deferred") or 0
        ) + int(evidence_rescue_status_counts.get("provider_deferred") or 0)
        reference_repair_file = first / "reference_repair.json"
        reference_repair = (
            json.loads(reference_repair_file.read_text(encoding="utf-8"))
            if reference_repair_file.is_file()
            else {"status": "not_requested", "changed": False}
        )
        deterministic_base_file = first / "deterministic_base_draft.md"
        deterministic_base_draft_text = (
            self._restore_artifact_urls(
                deterministic_base_file.read_text(encoding="utf-8"),
                dict(payload.get("figure_artifact_paths") or {}),
            )
            if deterministic_base_file.is_file()
            else str(payload.get("draft_text") or "")
        )
        overlay_file = first / "feedback_loop_rewrites.json"
        rewrite_overlays = (
            json.loads(overlay_file.read_text(encoding="utf-8"))
            if overlay_file.is_file()
            else dict(payload.get("rewrite_overlays") or {})
        )
        batch_review_file = first / "batch_review_candidates.json"
        batch_review = (
            json.loads(batch_review_file.read_text(encoding="utf-8"))
            if batch_review_file.is_file()
            else {}
        )
        result = {
            **evaluation,
            "fact_agent_repair": fact_repair,
            "score": float(evaluation.get("total_score") or 0),
            "goal": float(
                evaluation.get("pass_threshold") or payload.get("goal") or DRAFT_PASS_THRESHOLD
            ),
            "issues": issues,
            "hard_gate_failures": list(
                gate.get("hard_gate_failures")
                or evaluation.get("hard_gate_failures")
                or []
            ),
            "preflight": preflight,
            "source_check": source_check,
            "gate": gate,
            "feedback_status": feedback_status,
            "quality_reused": quality_reused,
            "run_mode": run_mode,
            "reference_repair": reference_repair,
            "deterministic_base_draft_text": deterministic_base_draft_text,
        }
        fact_checkpoint = (fact_repair.get("matrix_enrichment_checkpoint")
                           or (payload.get("prior_quality_context") or {}).get("fact_repair_checkpoint"))
        if fact_checkpoint:
            feedback_status["fact_repair_checkpoint"] = fact_checkpoint
        if not evaluate_only:
            result["draft_text"] = self._restore_artifact_urls(
                (first / "first_draft.md").read_text(encoding="utf-8"),
                dict(payload.get("figure_artifact_paths") or {}),
            )
            result["rewrite_overlays"] = rewrite_overlays
            if isinstance(batch_review, dict):
                result["review_candidate_draft_text"] = self._restore_artifact_urls(
                    str(batch_review.get("candidate_draft_text") or ""),
                    dict(payload.get("figure_artifact_paths") or {}),
                )
                result["review_candidate_score"] = batch_review.get(
                    "candidate_score"
                )
                result["review_changes"] = list(
                    batch_review.get("changes") or []
                )
                result["review_excluded"] = list(
                    batch_review.get("excluded") or []
                )
                result["review_candidate_full_draft_evaluated"] = bool(
                    batch_review.get("full_draft_evaluated")
                )
                result["review_candidate_quality"] = dict(
                    batch_review.get("full_draft_evaluation") or {}
                )
                result["review_source_quality"] = dict(
                    batch_review.get("source_evaluation") or {}
                )
        return result

    def draft_evaluate(self, context, payload):
        return self._draft_feedback(context, payload, evaluate_only=True)

    def draft_optimize(self, context, payload):
        return self._draft_feedback(context, payload, evaluate_only=False)

    @staticmethod
    def _retain_manual_confirmation_route(
        candidate_evaluation: dict[str, Any],
        generation_entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Prevent a style-only rewrite from clearing an evidence conflict."""

        if not bool(generation_entry.get("requires_manual_confirmation")):
            return candidate_evaluation
        result = dict(candidate_evaluation)
        paragraph_score = dict(result.get("paragraph_score") or {})
        paragraph_score["score"] = min(
            float(paragraph_score.get("score") or 0),
            79.0,
        )
        paragraph_score["severity"] = "major"
        paragraph_score["route"] = "human_confirmation"
        failed = [
            str(value)
            for value in paragraph_score.get("failed_dimensions") or []
            if str(value).strip()
        ]
        if "manual_source_confirmation" not in failed:
            failed.append("manual_source_confirmation")
        paragraph_score["failed_dimensions"] = failed
        paragraph_score["diagnosis"] = str(
            generation_entry.get("diagnosis")
            or paragraph_score.get("diagnosis")
            or "Manual source or figure-identity confirmation remains required."
        )
        result.update(
            {
                "paragraph_score": paragraph_score,
                "requires_manual_confirmation": True,
                "manual_confirmation_reason": paragraph_score["diagnosis"],
            }
        )
        return result

    def draft_rewrite(self, context, payload):
        """Generate one paragraph candidate, then score only that candidate."""

        if not str(payload.get("draft_text") or "").strip():
            raise WorkflowValidationError(
                "The rewrite task did not receive the current Draft content. "
                "Refresh the Draft and generate the rewrite candidate again."
            )
        fact_repair = {}  # Legacy result field; paragraph source rechecks own evidence recovery.
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="draft-workspace"
        )
        project_id = str(payload["project_id"])
        paragraph_id = str(payload["paragraph_id"])
        first = project / "04_first_draft"
        quality = dict(payload.get("quality") or {})
        goal = float(payload.get("goal") or quality.get("goal") or DRAFT_PASS_THRESHOLD)
        paragraph_goal = float(
            payload.get("paragraph_goal")
            or quality.get("paragraph_pass_threshold")
            or quality.get("paragraph_goal")
            or PARAGRAPH_PASS_THRESHOLD
        )
        min_case_words = int(
            payload.get("min_case_words") or CASE_PARAGRAPH_MIN_WORDS
        )
        max_case_words = int(
            payload.get("max_case_words") or CASE_PARAGRAPH_MAX_WORDS
        )
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, list):
            raw_issues = quality.get("issues") or []
        issues = [
            item
            for item in raw_issues
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ]
        normal, secrets = self._text_gateway_environment(context)
        paragraph_evaluation_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "paragraph_candidate_evaluation.json"
        )
        report_progress = getattr(context, "report_progress", None)
        finding = next(
            (
                dict(item)
                for item in quality.get("paragraph_scores") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        prior_issue = dict(issues[0]) if issues else {}
        if not finding:
            finding = dict(prior_issue)
        if not finding:
            raise RuntimeError(
                "The selected paragraph has no stored score or issue context. "
                "Evaluate the current Draft before generating a candidate."
            )
        finding = {
            **prior_issue,
            **finding,
            "paragraph_id": paragraph_id,
            "severity": str(
                finding.get("severity")
                or prior_issue.get("severity")
                or "minor"
            ),
            "route": str(
                finding.get("route")
                or prior_issue.get("route")
                or "section_rewrite"
            ),
            "diagnosis": str(
                finding.get("diagnosis")
                or prior_issue.get("diagnosis")
                or prior_issue.get("message")
                or "Polish this paragraph while preserving all protected facts and citations."
            ),
            "failed_dimensions": list(
                finding.get("failed_dimensions")
                or prior_issue.get("failed_dimensions")
                or []
            ),
        }

        source_paragraph_evaluation = {
            "evaluation_scope": "single_paragraph",
            "evaluation_mode": "stored_source_score",
            "paragraph_id": paragraph_id,
            "paragraph_score": finding,
        }

        evaluation = {
            **quality,
            "goal": goal,
            "paragraph_pass_threshold": paragraph_goal,
            "paragraph_scores": [finding],
            "paragraph_failures": [finding],
            "blocking_paragraph_failures": [finding],
        }
        self._write_json(first / "rubric_evaluation.json", evaluation)
        source_check = quality.get("source_check")
        source_check = source_check if isinstance(source_check, dict) else {}
        source_entry = next(
            (
                item
                for item in source_check.get("entries") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            None,
        )
        self._write_json(
            first / "original_source_check.json",
            {"entries": [source_entry]} if isinstance(source_entry, dict) and source_entry else {"entries": []},
        )
        preflight = quality.get("preflight")
        self._write_json(
            first / "first_draft_preflight.json",
            preflight if isinstance(preflight, dict) else {"paragraph_checks": []},
        )
        draft_path = first / "first_draft.md"
        digest = hashlib.sha256(draft_path.read_bytes()).hexdigest()
        self._write_json(
            first / "feedback_loop_status.json",
            {
                "status": "completed",
                "phase": "evaluated",
                "goal": goal,
                "paragraph_goal": paragraph_goal,
                "min_case_words": min_case_words,
                "max_case_words": max_case_words,
                "source_draft_sha256": digest,
                "output_draft_sha256": digest,
            },
        )
        relative_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "feedback_rewrite_candidates.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "propose_paragraph_rewrite.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--min-case-words",
                str(min_case_words),
                "--max-case-words",
                str(max_case_words),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        candidates = json.loads(
            (first / "feedback_rewrite_candidates.json").read_text(encoding="utf-8")
        )
        entry = (candidates.get("entries") or {}).get(paragraph_id)
        if not isinstance(entry, dict) or not str(entry.get("candidate_text") or "").strip():
            raise RuntimeError("The paragraph rewrite produced no candidate.")
        candidate_text = str(entry["candidate_text"]).strip()
        if callable(report_progress):
            report_progress(2, 4)

        original_draft = draft_path.read_text(encoding="utf-8")
        requested_paragraph = str(payload["paragraph_text"]).strip()
        # The proposal script is the final reader of the materialized Markdown.
        # Use the exact source span it rewrote for both replacement and the
        # second integrity check.  Mixing this value with an API/UI paragraph
        # projection previously compared a figure-bearing candidate against a
        # prose-only source, causing false image/metadata/number failures and
        # even duplicating the adjacent figure in the temporary scoring draft.
        original_paragraph = str(
            entry.get("original_text") or requested_paragraph
        ).strip()
        normalize = lambda value: " ".join(str(value or "").split())
        if normalize(original_paragraph) != normalize(requested_paragraph):
            raise RuntimeError(
                "The paragraph rewrite source boundary did not match the selected "
                "paragraph; the candidate was discarded before scoring."
            )
        if original_paragraph not in original_draft:
            raise RuntimeError(
                "The selected paragraph changed before candidate scoring."
            )
        candidate_draft = original_draft.replace(
            original_paragraph, candidate_text, 1
        )
        draft_path.write_text(candidate_draft, encoding="utf-8")
        self._write_json(
            first / "paragraph_candidate_evaluation_request.json",
            {
                "evaluation_mode": "accepted_candidate",
                "paragraph_id": paragraph_id,
                "original_text": original_paragraph,
                "source_corrections": entry.get('source_corrections') or [],
                "candidate_text": candidate_text,
                "allowed_unsupported_claims": list(
                    finding.get("unsupported_claims") or []
                ),
                "word_range_applicable": bool(
                    payload.get("word_range_applicable", True)
                ),
            },
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "evaluate_paragraph_candidate.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--goal",
                str(goal),
                "--paragraph-goal",
                str(paragraph_goal),
                "--min-case-words",
                str(min_case_words),
                "--max-case-words",
                str(max_case_words),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(paragraph_evaluation_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        candidate_evaluation = json.loads(
            (first / "paragraph_candidate_evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        candidate_evaluation = self._retain_manual_confirmation_route(
            candidate_evaluation,
            entry,
        )
        candidate_evaluation["fact_agent_repair"] = fact_repair
        if callable(report_progress):
            report_progress(3, 4)
        return {
            "candidate_text": candidate_text,
            "resolved_issue_ids": [
                str(item.get("issue_id") or item.get("id") or "")
                for item in issues
                if str(item.get("issue_id") or item.get("id") or "")
            ],
            "report": entry,
            "source_paragraph_evaluation": source_paragraph_evaluation,
            "candidate_evaluation": candidate_evaluation,
        }

    def draft_accept_rewrite(self, context, payload):
        """Evaluate only the accepted candidate paragraph in an isolated workspace."""

        staging, workspace, project = self._compatibility_workspace(
            context,
            payload,
            name="draft-workspace",
            markdown_key="candidate_draft_text",
        )
        project_id = str(payload["project_id"])
        paragraph_id = str(payload["paragraph_id"])
        first = project / "04_first_draft"
        self._write_json(
            first / "paragraph_candidate_evaluation_request.json",
            {
                "evaluation_mode": "accepted_candidate",
                "paragraph_id": paragraph_id,
                "original_text": str(payload["paragraph_text"]),
                "candidate_text": str(payload["candidate_text"]),
                "allowed_unsupported_claims": list(
                    payload.get("allowed_unsupported_claims") or []
                ),
                "word_range_applicable": bool(
                    payload.get("word_range_applicable", True)
                ),
            },
        )
        normal, secrets = self._text_gateway_environment(context)
        relative_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "paragraph_candidate_evaluation.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "evaluate_paragraph_candidate.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--goal",
                str(float(payload.get("goal") or DRAFT_PASS_THRESHOLD)),
                "--paragraph-goal",
                str(float(payload.get("paragraph_goal") or PARAGRAPH_PASS_THRESHOLD)),
                "--min-case-words",
                str(int(payload.get("min_case_words") or CASE_PARAGRAPH_MIN_WORDS)),
                "--max-case-words",
                str(int(payload.get("max_case_words") or CASE_PARAGRAPH_MAX_WORDS)),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        return json.loads(
            (first / "paragraph_candidate_evaluation.json").read_text(
                encoding="utf-8"
            )
        )
