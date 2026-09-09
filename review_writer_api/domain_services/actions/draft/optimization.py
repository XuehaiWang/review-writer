"""Optimization proposal actions mixed into DraftsService."""

from __future__ import annotations

import json
import hashlib
import uuid
from collections.abc import Callable
from copy import deepcopy
from functools import partial
from typing import Any

from review_writer_api.database import utc_now
from review_writer_api.domain_services.actions.draft.publication import repaired_artifact_metadata, repaired_evidence_content
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Principal
from review_writer_api.workflow_repository import ArtifactRecord
from review_writer_core.scientific_facts import evidence_repair_has_changes
from review_writer_core.draft_quality import (
    QUALITY_INPUT_ARTIFACTS,
    DEFAULT_SCORE_TOLERANCE,
    FULL_DRAFT_QUALITY_SCOPE,
    draft_text_sha256,
    full_draft_quality_mismatch_reasons,
    full_draft_quality_provenance,
    full_draft_score_gate,
    pending_claim_downgrade_paragraph_ids,
    quality_input_artifact_ids,
    quality_score,
)
from review_writer_core.writing_contracts import DRAFT_PASS_THRESHOLD
from review_writer_core.workflow.artifacts import (
    DRAFT_MANUSCRIPT as DRAFT_DOCUMENT,
    DRAFT_OPTIMIZATION_PROPOSALS as DRAFT_OPTIMIZATIONS,
    DRAFT_QUALITY_REPORT as DRAFT_QUALITY,
    DRAFT_REWRITE_OVERLAYS as DRAFT_OVERLAYS,
    MATRIX as MATRIX_LOGICAL_NAME,
    SECTION_EVIDENCE_PACKAGE as SECTION_EVIDENCE,
    SECTION_WRITING_PLAN,
)


class DraftOptimizationActionsMixin:
    def publish_optimization(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        expected_inputs = self.validate_task_inputs(principal, project_id, job_payload)
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        current_quality, current_quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        repaired_evidence, evidence_repair, claim_dispositions = (
            self._repair_evidence_package(job_payload, built)
        )
        self._validate_repair_lineages(principal, evidence_repair)
        candidate_matrix, matrix_fact_promotions = self._matrix_with_promoted_facts(
            dict(job_payload.get("matrix") or {}), evidence_repair
        )
        evidence_repair["matrix_fact_promotions"] = matrix_fact_promotions
        evidence_repair["matrix_fact_promotion_count"] = len(matrix_fact_promotions)
        merged_claim_dispositions = dict(
            current_quality.get("claim_dispositions") or {}
        )
        merged_claim_dispositions.update(claim_dispositions)
        claim_dispositions = merged_claim_dispositions
        deterministic_base_text = str(
            built.get("deterministic_base_draft_text") or current_text
        ).rstrip() + "\n"
        reference_repair = (
            dict(built.get("reference_repair") or {})
            if isinstance(built.get("reference_repair"), dict)
            else {"status": "not_requested", "changed": False}
        )
        review_changes = [
            dict(item)
            for item in built.get("review_changes") or []
            if isinstance(item, dict)
        ]
        review_candidate_text = str(
            built.get("review_candidate_draft_text") or ""
        )
        if review_changes and not bool(
            built.get("review_candidate_full_draft_evaluated")
        ):
            raise WorkflowConflict(
                "The combined optimization candidate was not evaluated as a full draft."
            )
        model_text = str(
            review_candidate_text
            if review_changes and review_candidate_text.strip()
            else built.get("draft_text")
            or ""
        ).rstrip() + "\n"
        if not model_text.strip():
            raise WorkflowValidationError("Batch optimization returned no Draft content.")
        score = max(
            0.0,
            min(
                float(
                    (
                        built.get("review_candidate_score")
                        if review_changes
                        else built.get("score")
                    )
                    or 0
                ),
                100.0,
            ),
        )
        quality_base = {
            key: value
            for key, value in built.items()
            if key
            not in {
                "draft_text",
                "rewrite_overlays",
                "source_draft_artifact_id",
                "review_candidate_draft_text",
                "review_candidate_score",
                "review_candidate_quality",
                "review_source_quality",
                "review_candidate_full_draft_evaluated",
                "review_changes",
                "review_excluded",
                "deterministic_base_draft_text",
                "reference_repair",
            }
        }
        routing_payload = {**job_payload, "section_evidence": repaired_evidence}
        if (built.get("rewrite_overlays") or {}).get("argument_revisions"):
            from review_writer_core.stages.draft.revisions import effective_writing_plan
            routing_payload["writing_plan"] = effective_writing_plan(
                job_payload.get("baseline_writing_plan") or job_payload.get("writing_plan") or {},
                built["rewrite_overlays"])
        routed_issues, routing = self._quality_routing(built, routing_payload)
        root_causes, repair_tasks = self._quality_root_causes(routed_issues)
        summary_source_quality = dict(current_quality or {})
        if not summary_source_quality.get("root_causes"):
            legacy_issues = [
                dict(row)
                for row in summary_source_quality.get("issues") or []
                if isinstance(row, dict)
            ]
            legacy_roots, _legacy_tasks = self._quality_root_causes(legacy_issues)
            summary_source_quality["root_causes"] = legacy_roots
        repair_summary = self._repair_summary(
            summary_source_quality,
            root_causes,
            evidence_repair=evidence_repair,
            reference_repair=reference_repair,
        )
        quality_base.update(
            {
                "issues": routed_issues,
                "routing": routing,
                "root_causes": root_causes,
                "repair_tasks": repair_tasks,
                "repair_summary": repair_summary,
                "score": score,
                "total_score": score,
                "goal": float(built.get("goal") or job_payload.get("goal") or DRAFT_PASS_THRESHOLD),
                "status": "completed",
                "quality_scope": "full_draft",
                "reference_repair": reference_repair,
                "evidence_repair": evidence_repair,
                "claim_dispositions": claim_dispositions,
                "evaluated_at": utc_now().isoformat(),
            }
        )
        feedback_status = (
            dict(built.get("feedback_status"))
            if isinstance(built.get("feedback_status"), dict)
            else {}
        )
        final_feedback_status = {
            **feedback_status,
            "phase": "completed",
            "full_draft_evaluated": bool(
                built.get("review_candidate_full_draft_evaluated")
                or not review_changes
            ),
        }
        quality_base["feedback_status"] = final_feedback_status
        quality_base.update(self._quality_status_partition(quality_base))

        # Only paragraph bodies may enter a batch proposal.  Rebuilding from
        # the current manuscript prevents a model from silently changing
        # headings, figure markers, references, or document structure outside
        # the reviewable paragraph comparisons.
        candidate_text, changes = self._optimization_candidate(
            deterministic_base_text, model_text
        )
        review_change_by_id = {
            str(item.get("paragraph_id") or ""): item
            for item in review_changes
            if str(item.get("paragraph_id") or "")
        }
        changes = [
            {
                **change,
                **{
                    key: value
                    for key, value in review_change_by_id.get(
                        str(change.get("paragraph_id") or ""), {}
                    ).items()
                    if key
                    not in {"paragraph_id", "original_text", "candidate_text"}
                },
            }
            for change in changes
        ]
        source_issue_ids: dict[str, list[str]] = {}
        for issue in current_quality.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            paragraph_id = str(issue.get("paragraph_id") or "")
            issue_id = str(issue.get("issue_id") or issue.get("id") or "")
            if paragraph_id and issue_id:
                source_issue_ids.setdefault(paragraph_id, []).append(issue_id)
        for change in changes:
            if change.get("target_issue_resolved"):
                change["resolved_issue_ids"] = source_issue_ids.get(
                    str(change.get("paragraph_id") or ""), []
                )
        quality_base.update(
            full_draft_quality_provenance(
                candidate_text,
                self._paragraph_spans(candidate_text),
                input_artifact_ids=quality_input_artifact_ids(job_payload),
            )
        )
        draft_changed = candidate_text != current_text
        evidence_changed = evidence_repair_has_changes(evidence_repair)
        deterministic_repair_changed = bool(reference_repair.get("changed"))

        if not draft_changed and not evidence_changed and not deterministic_repair_changed:
            # There is no text or evidence mutation to approve, but the exact
            # current manuscript was still fully evaluated.  Publish that
            # fresh, specifically routed Quality report instead of leaving an
            # older generic issue queue visible.
            quality = {
                **quality_base,
                "source_draft_artifact_id": current.id,
            }
            manual_review = self._manual_claim_review(current, quality)
            quality.update(
                {
                    "manual_claim_review": manual_review,
                    "verified_manual_paragraph_ids": manual_review[
                        "verified_manual_paragraph_ids"
                    ],
                    "unverified_manual_paragraph_ids": manual_review[
                        "unverified_manual_paragraph_ids"
                    ],
                }
            )
            quality.update(self._quality_status_partition(quality))
            expected_currents = dict(expected_inputs)
            with self._write_lock:
                published, state = self._publish_files(
                    principal,
                    project_id,
                    {
                        DRAFT_QUALITY: (
                            (
                                json.dumps(quality, ensure_ascii=False, indent=2)
                                + "\n"
                            ).encode(),
                            "json",
                        )
                    },
                    expected_revision=int(job_payload["expected_revision"]),
                    metadata={
                        "operation": "batch-optimization-full-evaluation",
                        "source_draft_artifact_id": current.id,
                    },
                    expected_current_artifacts=expected_currents,
                    invalidate_final=False,
                )
            return {
                "draft_artifact_id": current.id,
                "quality_artifact_id": published[DRAFT_QUALITY].id,
                "score": score,
                "draft_changed": False,
                "proposal_created": False,
                "rewrite_accepted": int(feedback_status.get("rewrite_accepted") or 0),
                "rewrite_rejected": int(feedback_status.get("rewrite_rejected") or 0),
                "rewrite_deferred": int(feedback_status.get("rewrite_deferred") or 0),
                "feedback_status": final_feedback_status,
                "repair_tasks": repair_tasks,
                "repair_status": str(repair_summary.get("repair_status") or "partial_success"),
                "revision": state.revision,
            }
        source_quality = dict(built.get("review_source_quality") or {})
        if source_quality:
            source_quality.update(
                {
                    "source_draft_artifact_id": current.id,
                    **full_draft_quality_provenance(
                        current_text,
                        self._paragraph_spans(current_text),
                        input_artifact_ids=quality_input_artifact_ids(job_payload),
                    ),
                }
            )
        elif current_quality:
            # Keep stored provenance unchanged.  An older paragraph-only
            # report must never be promoted to a full-draft baseline merely
            # because an optimization task consumed it.
            source_quality = dict(current_quality)
        if not source_quality:
            source_quality = {
                **quality_base,
                "source_draft_artifact_id": current.id,
            }
        source_score = quality_score(source_quality)
        source_quality["score"] = source_score
        source_quality.setdefault("total_score", source_score)
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS, required=False
        )
        entries = dict(store.get("entries") or {})
        proposal_id = str(uuid.uuid4())
        created_at = utc_now().isoformat()
        entries[proposal_id] = {
            **{field: job_payload[field] for field in QUALITY_INPUT_ARTIFACTS if field in job_payload},
            "proposal_id": proposal_id,
            "source_draft_artifact_id": current.id,
            "source_quality_artifact_id": (
                current_quality_artifact.id if current_quality_artifact else ""
            ),
            "candidate_draft_text": candidate_text,
            "deterministic_base_draft_text": deterministic_base_text,
            "reference_repair": reference_repair,
            "candidate_evidence_package": repaired_evidence,
            "candidate_matrix": candidate_matrix if matrix_fact_promotions else {},
            "evidence_repair": evidence_repair,
            "claim_dispositions": claim_dispositions,
            "candidate_quality": quality_base,
            "source_quality": source_quality,
            "score_gate": full_draft_score_gate(
                source_quality,
                quality_base,
                tolerance=DEFAULT_SCORE_TOLERANCE,
                source_text=current_text,
                candidate_text=candidate_text,
            ),
            "rewrite_overlays": (
                built.get("rewrite_overlays")
                if isinstance(built.get("rewrite_overlays"), dict)
                else (
                    job_payload.get("rewrite_overlays")
                    if isinstance(job_payload.get("rewrite_overlays"), dict)
                    else {}
                )
            ),
            "source_overlays": (
                dict(job_payload.get("rewrite_overlays") or {})
                if isinstance(job_payload.get("rewrite_overlays"), dict)
                else {}
            ),
            "changes": changes,
            "excluded": [
                dict(item)
                for item in built.get("review_excluded") or []
                if isinstance(item, dict)
            ],
            "source_score": source_score,
            "candidate_score": score,
            "feedback_status": final_feedback_status,
            "status": "pending",
            "created_at": created_at,
        }
        proposal_payload = {"project_id": project_id, "entries": entries}
        # Pure source recovery belongs to the unchanged current Draft. Publish
        # it with the proposal, never substitute a candidate's source check.
        paragraph_hashes = {p['paragraph_id']: hashlib.sha256(str(p['text']).encode()).hexdigest()
                            for p in self._paragraph_spans(current_text)}
        recovered_entries = [e for e in (source_quality.get('source_check') or {}).get('entries') or []
                             if (e.get('targeted_source_recheck') or {}).get('status') == 'supported_without_prose_change'
                             and e.get('source_check_status') == 'verified' and not e.get('unsupported_claims')
                             and not e.get('missing_core_claim_ids')
                             and e.get('paragraph_text_hash') == paragraph_hashes.get(e.get('paragraph_id'))
                             and e.get('paragraph_text_hash')]
        publish_files = {}
        if recovered_entries and deterministic_base_text == current_text.rstrip() + '\n':
            source_quality['issues'] = list(source_quality.get('paragraph_failures') or [])
            baseline_issues, baseline_routing = self._quality_routing(source_quality, job_payload)
            baseline_roots, baseline_tasks = self._quality_root_causes(baseline_issues)
            source_quality.update(issues=baseline_issues, routing=baseline_routing,
                                  root_causes=baseline_roots, repair_tasks=baseline_tasks,
                                  evaluated_at=utc_now().isoformat(), status='completed')
            source_quality.update(self._quality_status_partition(source_quality))
            publish_files[DRAFT_QUALITY] = ((json.dumps(source_quality, ensure_ascii=False) + '\n').encode(), 'json')
        def proposal_content(published):
            if DRAFT_QUALITY in published:
                entries[proposal_id]['source_quality_artifact_id'] = published[DRAFT_QUALITY].id
            return (json.dumps(proposal_payload, ensure_ascii=False, indent=2) + '\n').encode()
        publish_files[DRAFT_OPTIMIZATIONS] = (proposal_content if publish_files else proposal_content({}), 'json')
        expected_currents = dict(expected_inputs)
        if store_artifact is not None:
            expected_currents[DRAFT_OPTIMIZATIONS] = store_artifact.id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                publish_files,
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "batch-optimization-proposal",
                    "proposal_id": proposal_id,
                    "source_draft_artifact_id": current.id,
                    "feedback_status": final_feedback_status,
                    "evidence_repair": evidence_repair,
                    "reference_repair": reference_repair,
                },
                expected_current_artifacts=expected_currents,
                invalidate_final=False,
            )
        return {
            "draft_artifact_id": current.id,
            "quality_artifact_id": published[DRAFT_QUALITY].id if DRAFT_QUALITY in published else "",
            "score": score,
            "draft_changed": False,
            "proposal_created": True,
            "proposal_id": proposal_id,
            "proposal_artifact_id": published[DRAFT_OPTIMIZATIONS].id,
            "change_count": len(changes),
            "evidence_repair": evidence_repair,
            "reference_repair": reference_repair,
            "claim_dispositions": claim_dispositions,
            "rewrite_accepted": int(feedback_status.get("rewrite_accepted") or 0),
            "rewrite_rejected": int(feedback_status.get("rewrite_rejected") or 0),
            "rewrite_deferred": int(feedback_status.get("rewrite_deferred") or 0),
            "feedback_status": final_feedback_status,
            "repair_tasks": repair_tasks,
            "repair_status": str(repair_summary.get("repair_status") or "partial_success"),
            "revision": state.revision,
        }

    def auto_apply_optimization_proposal(
        self,
        principal: Principal,
        project_id: str,
        proposal_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Apply the worker's prevalidated safe subset when every gate passes.

        Unsafe candidates have already been moved to ``excluded`` before the
        proposal reaches this method.  A proposal is still held when its exact
        full-draft evaluation or any paragraph-level safety proof is missing.
        """

        store, _store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS
        )
        proposal = dict((store.get("entries") or {}).get(proposal_id) or {})
        if not proposal or proposal.get("status") != "pending":
            return {
                "auto_applied": False,
                "auto_apply_status": "proposal_not_pending",
            }
        current_text, current = self._read_text(
            principal, project_id, DRAFT_DOCUMENT
        )
        manual_ids = {
            str(item)
            for item in current.metadata.get("unverified_manual_paragraph_ids") or []
            if str(item).strip()
        }
        manual_ids.update(
            str(item)
            for item in (proposal.get("source_quality") or {}).get(
                "unverified_manual_paragraph_ids", []
            )
            if str(item).strip()
        )
        changes = [
            dict(item)
            for item in proposal.get("changes") or []
            if isinstance(item, dict) and item.get("paragraph_id")
        ]
        if any(change.get("requires_manual_confirmation") or change.get("argument_revisions") for change in changes):
            return {"auto_applied": False, "auto_apply_status": "manual_review_required"}
        unsafe: list[dict[str, Any]] = []
        candidate_quality = dict(proposal.get("candidate_quality") or {})
        source_quality = dict(proposal.get("source_quality") or {})
        downgrade_paragraph_ids = pending_claim_downgrade_paragraph_ids(
            proposal.get("claim_dispositions") or {}, source_quality
        )
        score_gate = dict(proposal.get("score_gate") or {})
        if not score_gate:
            score_gate = full_draft_score_gate(
                dict(proposal.get("source_quality") or {}),
                candidate_quality,
                tolerance=DEFAULT_SCORE_TOLERANCE,
            )
        if not bool(score_gate.get("allowed")):
            unsafe.append(
                {
                    "paragraph_id": "",
                    "reasons": list(score_gate.get("reasons") or ["full_draft_score_gate_failed"]),
                }
            )
        candidate_text = str(proposal.get("candidate_draft_text") or "")
        candidate_mismatches = full_draft_quality_mismatch_reasons(
            candidate_quality,
            draft_text=candidate_text,
            paragraphs=self._paragraph_spans(candidate_text),
            input_artifact_ids=dict(candidate_quality.get("input_artifact_ids") or {}),
        )
        source_mismatches = full_draft_quality_mismatch_reasons(
            source_quality,
            draft_text=current_text,
            paragraphs=self._paragraph_spans(current_text),
            input_artifact_ids=dict(source_quality.get("input_artifact_ids") or {}),
        )
        if str(source_quality.get("source_draft_artifact_id") or "") != current.id:
            source_mismatches.append("source_draft_artifact_mismatch")
        if candidate_mismatches:
            unsafe.append(
                {
                    "paragraph_id": "",
                    "reasons": [
                        "full_draft_evaluation_not_exact",
                        *candidate_mismatches,
                    ],
                }
            )
        if source_mismatches:
            unsafe.append(
                {
                    "paragraph_id": "",
                    "reasons": [
                        "full_draft_baseline_not_exact",
                        *source_mismatches,
                    ],
                }
            )
        for change in changes:
            paragraph_id = str(change.get("paragraph_id") or "")
            evaluation = dict(change.get("candidate_evaluation") or {})
            paragraph_score = dict(evaluation.get("paragraph_score") or {})
            preflight = dict(evaluation.get("local_preflight") or {})
            reasons: list[str] = []
            if paragraph_id in manual_ids:
                reasons.append("user_modified_paragraph")
            if bool(change.get("requires_manual_confirmation")) or bool(
                evaluation.get("requires_manual_confirmation")
            ):
                reasons.append("scientific_ambiguity_requires_confirmation")
            if evaluation.get("evaluation_scope") != "single_paragraph":
                reasons.append("paragraph_re_evaluation_missing")
            if evaluation.get("local_hard_gate_failures"):
                reasons.append("local_integrity_failure")
            if preflight.get("hard_regressions"):
                reasons.append("local_preflight_regression")
            if str(paragraph_score.get("route") or "") == "human_confirmation":
                reasons.append("human_confirmation_route")
            if paragraph_id in downgrade_paragraph_ids and not bool(
                change.get("accuracy_improved")
            ):
                reasons.append("claim_downgrade_did_not_improve_evidence_accuracy")
            if change.get("target_issue_resolved") is False:
                reasons.append("target_issue_not_resolved")
            if change.get("introduced_issue_ids"):
                reasons.append("candidate_introduced_new_issues")
            try:
                source_score = float(change.get("source_paragraph_score"))
                candidate_score = float(change.get("candidate_paragraph_score"))
            except (TypeError, ValueError):
                if not bool(change.get("accuracy_improved")):
                    reasons.append("score_delta_unavailable")
            else:
                if candidate_score < source_score - DEFAULT_SCORE_TOLERANCE:
                    reasons.append("paragraph_score_regression")
            if reasons:
                unsafe.append({"paragraph_id": paragraph_id, "reasons": reasons})

        reference_repair = dict(proposal.get("reference_repair") or {})
        evidence_repair = dict(proposal.get("evidence_repair") or {})
        has_deterministic_repairs = bool(
            reference_repair.get("changed")
            or evidence_repair_has_changes(evidence_repair)
        )
        changed_paragraph_ids = {
            str(change.get("paragraph_id") or "") for change in changes
        }
        missing_downgrade_rewrites = sorted(
            downgrade_paragraph_ids - changed_paragraph_ids
        )
        if missing_downgrade_rewrites:
            unsafe.extend(
                {
                    "paragraph_id": paragraph_id,
                    "reasons": ["unsupported_claim_was_not_downgraded_in_text"],
                }
                for paragraph_id in missing_downgrade_rewrites
            )
        if (not changes and not has_deterministic_repairs) or unsafe:
            globally_blocked = any(
                not str(item.get("paragraph_id") or "") for item in unsafe
            )
            unsafe_paragraph_ids = {
                str(item.get("paragraph_id") or "")
                for item in unsafe
                if str(item.get("paragraph_id") or "")
            }
            return {
                "auto_applied": False,
                "auto_apply_status": "manual_review_required",
                "manual_review_reasons": unsafe,
                "safe_paragraph_ids": (
                    []
                    if globally_blocked
                    else sorted(
                        str(change.get("paragraph_id") or "")
                        for change in changes
                        if str(change.get("paragraph_id") or "")
                        not in unsafe_paragraph_ids
                    )
                ),
            }
        accepted = self.decide_optimization_proposal(
            principal,
            project_id,
            proposal_id,
            decision="accept",
            revision=revision,
            selected_paragraph_ids=[
                str(change.get("paragraph_id") or "") for change in changes
            ],
        )
        return {
            **accepted,
            "auto_applied": True,
            "auto_apply_status": "all_safe_paragraphs_applied",
            "proposal_created": False,
            "draft_changed": bool(accepted.get("draft_changed", True)),
            "resolved_issue_count": sum(
                len(change.get("resolved_issue_ids") or []) for change in changes
            ),
            "unresolved_issue_count": len(proposal.get("excluded") or []),
        }

    def decide_optimization_proposal(
        self,
        principal: Principal,
        project_id: str,
        proposal_id: str,
        *,
        decision: str,
        revision: int,
        selected_paragraph_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise WorkflowValidationError("Unknown optimization proposal decision.")
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS
        )
        entries = dict(store.get("entries") or {})
        proposal = dict(entries.get(proposal_id) or {})
        if not proposal:
            raise WorkflowNotFound("Optimization proposal not found.")
        if proposal.get("status") != "pending":
            raise WorkflowConflict("Optimization proposal was already decided.")
        current_text, current = self._read_text(
            principal, project_id, DRAFT_DOCUMENT
        )
        if proposal.get("source_draft_artifact_id") != current.id:
            raise WorkflowConflict("Optimization proposal is stale for the current Draft.")

        proposal_changes = [
            dict(item)
            for item in proposal.get("changes") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "").strip()
        ]
        available_ids = {
            str(item.get("paragraph_id") or "") for item in proposal_changes
        }
        requested_ids = [
            str(value).strip()
            for value in selected_paragraph_ids or []
            if str(value).strip()
        ]
        reference_repair = dict(proposal.get("reference_repair") or {})
        evidence_repair = dict(proposal.get("evidence_repair") or {})
        has_automatic_repairs = bool(
            reference_repair.get("changed")
            or evidence_repair_has_changes(evidence_repair)
        )
        expected_inputs = {}
        if decision == "accept":
            expected_inputs = self.validate_task_inputs(principal, project_id, proposal)
            selected_ids = set(requested_ids or sorted(available_ids))
            unknown = sorted(selected_ids - available_ids)
            if unknown:
                raise WorkflowValidationError(
                    "Unknown optimization paragraph selection: "
                    + ", ".join(unknown)
                )
            if not selected_ids and not has_automatic_repairs:
                raise WorkflowValidationError(
                    "Select at least one optimized paragraph to save."
                )
        else:
            selected_ids = set()
        selected_changes = [
            item
            for item in proposal_changes
            if str(item.get("paragraph_id") or "") in selected_ids
        ]

        if decision == "accept":
            from review_writer_core.stages.draft.revisions import accept_argument_revisions
            try:
                accepted_overlays = accept_argument_revisions(
                    proposal.get("source_overlays") or {}, proposal.get("rewrite_overlays") or {},
                    selected_ids, proposal_changes)
            except ValueError as exc:
                raise WorkflowValidationError(str(exc)) from exc
            if selected_ids != available_ids:
                # Replay the same deterministic repair on current artifacts,
                # using only accepted paragraph checks; never publish the
                # whole batch's evidence or Matrix for a partial acceptance.
                if evidence_repair_has_changes(evidence_repair):
                    payload = self.compatibility_payload(principal, project_id)
                    quality = proposal.get("candidate_quality") or {}
                    scores = {row["paragraph_id"]: row for row in quality.get("paragraph_scores") or []}
                    checks = {row["paragraph_id"]: row for row in (quality.get("source_check") or {}).get("entries") or []}
                    for change in selected_changes:
                        evaluation = change.get("candidate_evaluation") or {}
                        pid = str(change["paragraph_id"])
                        if evaluation.get("paragraph_score"):
                            scores[pid] = {**evaluation["paragraph_score"], "paragraph_id": pid}
                        if evaluation.get("source_check_entry"):
                            checks[pid] = {**evaluation["source_check_entry"], "paragraph_id": pid}
                    candidate_evidence, evidence_repair, _dispositions = self._repair_evidence_package(
                        payload, {
                            "paragraph_scores": [row for pid, row in scores.items() if pid in selected_ids],
                            "source_check": {"entries": [row for pid, row in checks.items() if pid in selected_ids]},
                            "fact_agent_repair": quality.get("fact_agent_repair") or {},
                            "review_changes": selected_changes,
                        },
                    )
                    candidate_matrix, promotions = self._matrix_with_promoted_facts(
                        payload.get("matrix") or {}, evidence_repair,
                    )
                    evidence_repair.update(matrix_fact_promotions=promotions, matrix_fact_promotion_count=len(promotions))
                    proposal.update(candidate_evidence_package=candidate_evidence,
                                    candidate_matrix=candidate_matrix, evidence_repair=evidence_repair)
                dispositions = dict((proposal.get("source_quality") or {}).get("claim_dispositions") or {})
                dispositions.update({key: row for key, row in (proposal.get("claim_dispositions") or {}).items()
                                     if str(row.get("paragraph_id") or "") in selected_ids})
                proposal["claim_dispositions"] = dispositions
            self._validate_repair_lineages(principal, evidence_repair)
            has_automatic_repairs = bool(reference_repair.get("changed") or evidence_repair_has_changes(evidence_repair))

        decided_at = utc_now().isoformat()
        proposal["status"] = "accepted" if decision == "accept" else "rejected"
        proposal["decided_at"] = decided_at
        proposal["selected_paragraph_ids"] = sorted(selected_ids)
        proposal["discarded_paragraph_ids"] = sorted(available_ids - selected_ids)
        entries[proposal_id] = proposal
        if decision == "accept":
            for other_id, other_value in list(entries.items()):
                if other_id == proposal_id or not isinstance(other_value, dict):
                    continue
                other = dict(other_value)
                if (
                    other.get("status") == "pending"
                    and other.get("source_draft_artifact_id") == current.id
                ):
                    other["status"] = "superseded"
                    other["superseded_by_proposal_id"] = proposal_id
                    other["decided_at"] = decided_at
                    entries[other_id] = other

        files: dict[
            str,
            tuple[bytes | Callable[[dict[str, ArtifactRecord]], bytes], str],
        ] = {
            DRAFT_OPTIMIZATIONS: (
                (
                    json.dumps(
                        {"project_id": project_id, "entries": entries},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode(),
                "json",
            )
        }
        if decision == "accept":
            candidate_matrix = dict(proposal.get("candidate_matrix") or {})
            if (
                evidence_repair.get("matrix_fact_promotion_count")
                and candidate_matrix
            ):
                files[MATRIX_LOGICAL_NAME] = (
                    (
                        json.dumps(candidate_matrix, ensure_ascii=False, indent=2)
                        + "\n"
                    ).encode(),
                    "json",
                )
            candidate_evidence = dict(
                proposal.get("candidate_evidence_package") or {}
            )
            if evidence_repair_has_changes(evidence_repair) and candidate_evidence:
                files[SECTION_EVIDENCE] = (partial(repaired_evidence_content, candidate_evidence), "json")
            deterministic_base = str(
                proposal.get("deterministic_base_draft_text") or current_text
            )
            candidate_text = self._optimization_candidate_from_changes(
                deterministic_base, selected_changes
            )
            if not candidate_text.strip() or (
                candidate_text == current_text and not has_automatic_repairs
            ):
                raise WorkflowConflict("Optimization proposal contains no applicable change.")
            files[DRAFT_DOCUMENT] = (
                (candidate_text.rstrip() + "\n").encode("utf-8"),
                "markdown",
            )

            exact_full_quality = dict(proposal.get("candidate_quality") or {})
            exact_full_selection = bool(
                selected_ids == available_ids
                and str(exact_full_quality.get("quality_scope") or "")
                == FULL_DRAFT_QUALITY_SCOPE
                and str(exact_full_quality.get("evaluation_input_sha256") or "")
                == draft_text_sha256(candidate_text)
            )
            if exact_full_selection:
                # The optimization worker evaluated these exact combined bytes.
                # Publish that result directly; paragraph scores remain trace
                # data and must not replace the authoritative full evaluation.
                candidate_quality = exact_full_quality
                quality_scope = FULL_DRAFT_QUALITY_SCOPE
            else:
                candidate_quality, scored_changes = (
                    self._optimization_quality_from_scored_changes(
                        proposal, selected_changes
                    )
                )
                quality_scope = "batch_selected_paragraphs"
            evaluation_complete = bool(
                exact_full_selection or scored_changes == len(selected_changes)
            )
            candidate_quality.update(
                {
                    "quality_scope": quality_scope,
                    "requires_full_draft_refresh": not exact_full_selection,
                    "selection_evaluation_complete": evaluation_complete,
                    "evaluation_input_sha256": draft_text_sha256(candidate_text),
                    "paragraph_coverage": [
                        str(row.get("paragraph_id") or "")
                        for row in self._paragraph_spans(candidate_text)
                        if str(row.get("paragraph_id") or "")
                    ],
                    "selected_paragraph_ids": sorted(selected_ids),
                    "reference_repair": reference_repair,
                    "evidence_repair": evidence_repair,
                    "claim_dispositions": dict(
                        proposal.get("claim_dispositions") or {}
                    ),
                    "status": "completed",
                    "evaluated_at": decided_at,
                }
            )
            manual_review = self._manual_claim_review(current, candidate_quality)
            candidate_quality.update(
                {
                    "manual_claim_review": manual_review,
                    "verified_manual_paragraph_ids": manual_review[
                        "verified_manual_paragraph_ids"
                    ],
                    "unverified_manual_paragraph_ids": manual_review[
                        "unverified_manual_paragraph_ids"
                    ],
                }
            )
            candidate_quality.update(
                self._quality_status_partition(candidate_quality)
            )

            quality_inputs = {
                **dict(getattr(current, "metadata", {}) or {}),
                **proposal,
            }

            def quality_content(published: dict[str, ArtifactRecord]) -> bytes:
                input_artifact_ids = quality_input_artifact_ids(quality_inputs)
                for logical_name, source_key in (
                    (MATRIX_LOGICAL_NAME, "source_matrix_artifact_id"),
                    (SECTION_EVIDENCE, "source_section_evidence_artifact_id"),
                    (DRAFT_OVERLAYS, "source_rewrite_overlay_artifact_id"),
                ):
                    if logical_name in published:
                        input_artifact_ids[source_key] = published[logical_name].id
                quality = {
                    **candidate_quality,
                    "source_draft_artifact_id": published[DRAFT_DOCUMENT].id,
                    "status": "completed",
                    "evaluated_at": decided_at,
                    **(
                        full_draft_quality_provenance(
                            candidate_text,
                            self._paragraph_spans(candidate_text),
                            input_artifact_ids=input_artifact_ids,
                        )
                        if exact_full_selection
                        else {
                            "quality_scope": "batch_selected_paragraphs",
                            "evaluation_input_sha256": draft_text_sha256(candidate_text),
                        }
                    ),
                }
                return (
                    json.dumps(quality, ensure_ascii=False, indent=2) + "\n"
                ).encode()

            rewrite_overlays = accepted_overlays
            for cid, revision_value in (rewrite_overlays.get("argument_revisions") or {}).items():
                if revision_value != (proposal.get("source_overlays") or {}).get("argument_revisions", {}).get(cid):
                    revision_value.update(accepted_at=decided_at, proposal_id=proposal_id)
            overlay_entries = dict(rewrite_overlays.get("entries") or {})
            for change in selected_changes:
                paragraph_id = str(change.get("paragraph_id") or "")
                overlay_entries[paragraph_id] = {
                    "paragraph_id": paragraph_id,
                    "source_text_sha256": (overlay_entries.get(paragraph_id) or {}).get("source_text_sha256") or self._text_sha256(
                        str(change.get("original_text") or "")
                    ),
                    "rewritten_text": str(change.get("candidate_text") or ""),
                    "updated_at": decided_at,
                }
            rewrite_overlays.update(
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "entries": overlay_entries,
                }
            )
            files[DRAFT_OVERLAYS] = (
                (
                    json.dumps(
                        rewrite_overlays, ensure_ascii=False, indent=2
                    )
                    + "\n"
                ).encode(),
                "json",
            )
            # Quality is inserted last so its provenance callback can bind the
            # newly published Matrix, Evidence and Overlay artifact identities.
            files[DRAFT_QUALITY] = (quality_content, "json")

        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "batch-optimization-proposal",
            "subject_id": proposal_id,
            "decision": decision,
            "details": {
                "change_count": len(selected_changes),
                "selected_paragraph_ids": sorted(selected_ids),
                "source_draft_artifact_id": current.id,
                "candidate_score": proposal.get("candidate_score"),
            },
            "created_at": utc_now(),
        }
        common_metadata = {
            **dict(current.metadata),
            "operation": f"batch-optimization-{decision}",
            "proposal_id": proposal_id,
            "previous_draft_artifact_id": current.id,
            "reference_repair": reference_repair,
            "evidence_repair": evidence_repair,
        }

        expected_currents = {
            DRAFT_DOCUMENT: current.id,
            DRAFT_OPTIMIZATIONS: store_artifact.id,
        }
        expected_currents.update(expected_inputs)
        for logical_name, source_key in (
            (DRAFT_QUALITY, "source_quality_artifact_id"),
            (SECTION_EVIDENCE, "source_section_evidence_artifact_id"),
            (MATRIX_LOGICAL_NAME, "source_matrix_artifact_id"),
            (SECTION_WRITING_PLAN, "source_writing_plan_artifact_id"),
            (DRAFT_OVERLAYS, "source_rewrite_overlay_artifact_id"),
        ):
            source_id = str(proposal.get(source_key) or "")
            if source_id:
                expected_currents[logical_name] = source_id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=revision,
                metadata=common_metadata,
                metadata_builder=partial(repaired_artifact_metadata, common_metadata),
                approval_events=[event],
                expected_current_artifacts=expected_currents,
                invalidate_final=decision == "accept",
            )
        return {
            "proposal_id": proposal_id,
            "decision": decision,
            "selected_paragraph_ids": sorted(selected_ids),
            "draft_artifact_id": (
                published[DRAFT_DOCUMENT].id
                if DRAFT_DOCUMENT in published
                else current.id
            ),
            "quality_artifact_id": (
                published[DRAFT_QUALITY].id
                if DRAFT_QUALITY in published
                else ""
            ),
            "section_evidence_artifact_id": (
                published[SECTION_EVIDENCE].id
                if SECTION_EVIDENCE in published
                else str(proposal.get("source_section_evidence_artifact_id") or "")
            ),
            "matrix_artifact_id": (
                published[MATRIX_LOGICAL_NAME].id
                if MATRIX_LOGICAL_NAME in published
                else str(proposal.get("source_matrix_artifact_id") or "")
            ),
            "evidence_repair": evidence_repair,
            "reference_repair": reference_repair,
            "draft_changed": bool(
                decision == "accept" and DRAFT_DOCUMENT in published
            ),
            "score": (
                candidate_quality.get("score")
                if decision == "accept"
                else None
            ),
            "revision": state.revision,
        }

    def repair_accepted_optimization_quality(
        self,
        principal: Principal,
        project_id: str,
        proposal_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Republish a score that was saved from the old all-selected path.

        This is intentionally a service-level maintenance operation, not a
        public route.  It lets deployments repair an already accepted proposal
        without another paid model evaluation or any manuscript rewrite.
        """

        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_OPTIMIZATIONS
        )
        proposal = dict((store.get("entries") or {}).get(proposal_id) or {})
        if not proposal or proposal.get("status") != "accepted":
            raise WorkflowConflict("Accepted optimization proposal not found.")
        current_text, current = self._read_text(
            principal, project_id, DRAFT_DOCUMENT
        )
        if str(current.metadata.get("proposal_id") or "") != proposal_id:
            raise WorkflowConflict(
                "The accepted proposal is not attached to the current Draft."
            )
        _quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY
        )
        selected_ids = {
            str(value)
            for value in proposal.get("selected_paragraph_ids") or []
            if str(value).strip()
        }
        selected_changes = [
            dict(item)
            for item in proposal.get("changes") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") in selected_ids
        ]
        candidate_quality, scored_changes = (
            self._optimization_quality_from_scored_changes(
                proposal, selected_changes
            )
        )
        if not selected_changes or scored_changes != len(selected_changes):
            raise WorkflowConflict(
                "The accepted proposal has no complete paragraph-level scores."
            )
        repaired_at = utc_now().isoformat()
        candidate_quality.update(
            {
                "quality_scope": "batch_selected_paragraphs",
                "requires_full_draft_refresh": True,
                "evaluation_input_sha256": draft_text_sha256(current_text),
                "paragraph_coverage": [
                    str(row.get("paragraph_id") or "")
                    for row in self._paragraph_spans(current_text)
                    if str(row.get("paragraph_id") or "")
                ],
                "selected_paragraph_ids": sorted(selected_ids),
                "reference_repair": dict(proposal.get("reference_repair") or {}),
                "evidence_repair": dict(proposal.get("evidence_repair") or {}),
                "claim_dispositions": dict(
                    proposal.get("claim_dispositions") or {}
                ),
                "status": "completed",
                "evaluated_at": repaired_at,
            }
        )
        manual_review = self._manual_claim_review(current, candidate_quality)
        candidate_quality.update(
            {
                "manual_claim_review": manual_review,
                "verified_manual_paragraph_ids": manual_review[
                    "verified_manual_paragraph_ids"
                ],
                "unverified_manual_paragraph_ids": manual_review[
                    "unverified_manual_paragraph_ids"
                ],
            }
        )
        candidate_quality.update(self._quality_status_partition(candidate_quality))
        candidate_quality["source_draft_artifact_id"] = current.id
        metadata = {
            **dict(current.metadata),
            "operation": "repair-batch-optimization-quality",
            "proposal_id": proposal_id,
            "source_draft_artifact_id": current.id,
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_QUALITY: (
                        (
                            json.dumps(
                                candidate_quality, ensure_ascii=False, indent=2
                            )
                            + "\n"
                        ).encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                metadata=metadata,
                expected_current_artifacts={
                    DRAFT_DOCUMENT: current.id,
                    DRAFT_QUALITY: quality_artifact.id,
                    DRAFT_OPTIMIZATIONS: store_artifact.id,
                },
                invalidate_final=True,
            )
        return {
            "proposal_id": proposal_id,
            "draft_artifact_id": current.id,
            "quality_artifact_id": published[DRAFT_QUALITY].id,
            "score": candidate_quality.get("score"),
            "revision": state.revision,
        }
