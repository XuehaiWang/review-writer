"""Paragraph rewrite actions mixed into DraftsService."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from copy import deepcopy
from functools import partial
from typing import Any

from review_writer_api.database import utc_now
from review_writer_api.domain_services.actions.draft.publication import repaired_artifact_metadata, repaired_evidence_content
from review_writer_api.domain_services.actions.draft.errors import DraftNotReady
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Principal
from review_writer_api.workflow_repository import ArtifactRecord
from review_writer_core.draft_quality import QUALITY_INPUT_ARTIFACTS
from review_writer_core.draft_issue_routing import draft_fact_repair_checkpoint
from review_writer_core.scientific_facts import evidence_repair_has_changes
from review_writer_core.writing_contracts import (
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    DRAFT_PASS_THRESHOLD,
    PARAGRAPH_PASS_THRESHOLD,
)
from review_writer_core.workflow.artifacts import (
    DRAFT_MANUSCRIPT as DRAFT_DOCUMENT,
    DRAFT_QUALITY_REPORT as DRAFT_QUALITY,
    DRAFT_REWRITE_CANDIDATES as DRAFT_REWRITES,
    DRAFT_REWRITE_OVERLAYS as DRAFT_OVERLAYS,
    MATRIX as MATRIX_LOGICAL_NAME,
    SECTION_EVIDENCE_PACKAGE as SECTION_EVIDENCE,
)


class DraftRewriteActionsMixin:
    def rewrite_payload(
        self, principal: Principal, project_id: str, paragraph_id: str
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        quality = payload.get("quality") or {}
        if not payload.get("quality_artifact_id") or not quality:
            raise DraftNotReady(
                "Evaluate the Draft once before requesting a paragraph rewrite."
            )
        paragraph = next(
            (row for row in payload["paragraphs"] if row["paragraph_id"] == paragraph_id),
            None,
        )
        if paragraph is None:
            raise WorkflowNotFound("Draft paragraph not found.")
        matching_issues = [
            issue
            for issue in quality.get("issues") or []
            if issue.get("paragraph_id") == paragraph_id
        ]
        if not matching_issues:
            raise DraftNotReady("This paragraph is not in the current issue queue.")
        if not any(issue.get("interactive_rewrite_eligible", issue.get("rewrite_eligible", True)) for issue in matching_issues):
            repair_stages = sorted(
                {
                    str(issue.get("repair_stage") or "upstream")
                    for issue in matching_issues
                }
            )
            raise DraftNotReady(
                "This issue cannot be fixed safely by rewriting prose. "
                "Use the routed repair stage instead: " + ", ".join(repair_stages)
            )
        preflight = quality.get("preflight")
        preflight = preflight if isinstance(preflight, dict) else {}
        paragraph_check = next(
            (
                item
                for item in preflight.get("paragraph_checks") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        case_range = preflight.get("case_word_range")
        if isinstance(case_range, (list, tuple)) and len(case_range) >= 2:
            min_case_words = int(case_range[0] or CASE_PARAGRAPH_MIN_WORDS)
            max_case_words = int(case_range[1] or CASE_PARAGRAPH_MAX_WORDS)
        elif isinstance(case_range, dict):
            min_case_words = int(
                case_range.get("min_words") or CASE_PARAGRAPH_MIN_WORDS
            )
            max_case_words = int(
                case_range.get("max_words") or CASE_PARAGRAPH_MAX_WORDS
            )
        else:
            min_case_words, max_case_words = (
                CASE_PARAGRAPH_MIN_WORDS,
                CASE_PARAGRAPH_MAX_WORDS,
            )
        compatibility = self.compatibility_payload(principal, project_id)
        return {
            **compatibility,
            "project_id": project_id,
            # The native rewrite handler materializes ``first_draft.md`` from
            # this field before invoking the feedback-loop CLI.  Keeping only
            # ``paragraph_text`` is insufficient because the CLI validates the
            # selected paragraph against the complete, evaluated draft.
            "draft_text": payload["first_draft_md"],
            "paragraph_id": paragraph_id,
            "paragraph_text": paragraph["text"],
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "expected_revision": payload["revision"],
            "quality": quality,
            "prior_quality_context": {"fact_repair_checkpoint": draft_fact_repair_checkpoint(payload)},
            "issues": matching_issues,
            "goal": float(quality.get("goal") or quality.get("pass_threshold") or DRAFT_PASS_THRESHOLD),
            "paragraph_goal": float(
                quality.get("paragraph_pass_threshold")
                or quality.get("paragraph_goal")
                or PARAGRAPH_PASS_THRESHOLD
            ),
            "min_case_words": min_case_words,
            "max_case_words": max_case_words,
            "word_range_applicable": bool(
                paragraph_check.get("word_range_applicable", True)
            ),
        }

    def publish_rewrite_candidate(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        if job_payload.get("revision_mode") == "dialogue":
            return self.publish_dialogue(principal, project_id, job_payload, built)
        expected_inputs = self.validate_task_inputs(principal, project_id, job_payload)
        current = self._artifact(principal, project_id, DRAFT_DOCUMENT)
        current_quality = self._artifact(principal, project_id, DRAFT_QUALITY)
        original = str(job_payload["paragraph_text"])
        paragraph_id = str(job_payload["paragraph_id"])
        source_paragraph_evaluation = dict(
            built.get("source_paragraph_evaluation") or {}
        )
        candidate_evaluation = dict(built.get("candidate_evaluation") or {})
        candidate_score_entry = dict(candidate_evaluation.get("paragraph_score") or {})
        if (
            candidate_evaluation.get("evaluation_scope") != "single_paragraph"
            or str(candidate_evaluation.get("paragraph_id") or "") != paragraph_id
            or str(candidate_score_entry.get("paragraph_id") or "") != paragraph_id
        ):
            raise WorkflowValidationError(
                "The generated candidate did not receive a valid paragraph-only score."
            )
        source_score_entry = dict(
            source_paragraph_evaluation.get("paragraph_score") or {}
        )
        if str(source_score_entry.get("paragraph_id") or "") != paragraph_id:
            raise WorkflowValidationError(
                "The candidate comparison is missing the source paragraph score."
            )
        try:
            source_paragraph_score = float(source_score_entry["score"])
            candidate_paragraph_score = float(candidate_score_entry["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowValidationError(
                "The candidate comparison contains an invalid paragraph score."
            ) from exc
        candidate_text = str(built.get("candidate_text") or "").strip()
        if not candidate_text:
            raise WorkflowValidationError("AI rewrite returned no candidate text.")
        if self._normalized(candidate_text) == self._normalized(original):
            raise WorkflowValidationError(
                "AI rewrite made no normalized content change; it was not accepted as a candidate."
            )
        _candidate_evidence, evidence_repair_preview, claim_dispositions_preview = (
            self._single_paragraph_evidence_repair(
                job_payload,
                candidate_evaluation,
                source_paragraph_evaluation,
            )
        )
        self._validate_repair_lineages(principal, evidence_repair_preview)
        store, _artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES, required=False
        )
        expected_inputs[DRAFT_REWRITES] = _artifact.id if _artifact else ""
        entries = dict(store.get("entries") or {})
        candidate_id = str(uuid.uuid4())
        entries[candidate_id] = {
            **{field: job_payload[field] for field in QUALITY_INPUT_ARTIFACTS if field in job_payload},
            "candidate_id": candidate_id,
            "paragraph_id": job_payload["paragraph_id"],
            "source_draft_artifact_id": current.id,
            "source_quality_artifact_id": current_quality.id,
            "original_text": original,
            "candidate_text": candidate_text,
            "candidate_text_sha256": self._text_sha256(candidate_text),
            "resolved_issue_ids": list(built.get("resolved_issue_ids") or []),
            "generation_report": dict(built.get("report") or {}),
            "route": str(dict(built.get("report") or {}).get("route") or ""),
            "rewrite_mode": str(
                dict(built.get("report") or {}).get("rewrite_mode") or ""
            ),
            "requires_manual_confirmation": bool(
                dict(built.get("report") or {}).get(
                    "requires_manual_confirmation", False
                )
            ),
            "source_paragraph_evaluation": source_paragraph_evaluation,
            "candidate_evaluation": candidate_evaluation,
            "evidence_repair_preview": evidence_repair_preview,
            "claim_dispositions_preview": claim_dispositions_preview,
            "source_paragraph_score": round(source_paragraph_score, 2),
            "candidate_paragraph_score": round(candidate_paragraph_score, 2),
            "status": "pending",
            "created_at": utc_now().isoformat(),
        }
        payload = {"project_id": project_id, "entries": entries}
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_REWRITES: (
                        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=self._revision(principal, project_id),
                metadata={"operation": "rewrite-candidate", "paragraph_id": job_payload["paragraph_id"]},
                expected_current_artifacts=expected_inputs,
                invalidate_final=False,
            )
        return {
            "candidate_id": candidate_id,
            "candidate_artifact_id": published[DRAFT_REWRITES].id,
            "paragraph_id": paragraph_id,
            "source_paragraph_score": round(source_paragraph_score, 2),
            "candidate_paragraph_score": round(candidate_paragraph_score, 2),
            "evidence_repair_preview": evidence_repair_preview,
            "revision": state.revision,
        }

    def accept_rewrite_payload(
        self,
        principal: Principal,
        project_id: str,
        candidate_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Build an immutable payload for publishing one scored candidate."""

        payload = self.get(principal, project_id)
        quality = dict(payload.get("quality") or {})
        if not payload.get("quality_artifact_id") or not quality:
            raise DraftNotReady("The rewrite candidate has no evaluation context.")
        if int(payload.get("revision") or 0) != int(revision):
            raise WorkflowConflict("Draft revision changed. Refresh and try again.")
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES
        )
        candidate = dict((store.get("entries") or {}).get(candidate_id) or {})
        if not candidate:
            raise WorkflowNotFound("Rewrite candidate not found.")
        if candidate.get("status") != "pending":
            raise WorkflowConflict("Rewrite candidate was already decided.")
        if candidate.get("source_draft_artifact_id") != payload["draft_artifact_id"]:
            raise WorkflowConflict("Rewrite candidate is stale for the current Draft.")
        if candidate.get("source_quality_artifact_id") != payload["quality_artifact_id"]:
            raise WorkflowConflict("Rewrite candidate is stale for the current evaluation.")
        paragraph_id = str(candidate.get("paragraph_id") or "")
        paragraph = next(
            (
                row
                for row in self._paragraph_spans(payload["first_draft_md"])
                if row["paragraph_id"] == paragraph_id
            ),
            None,
        )
        if paragraph is None or self._normalized(paragraph["text"]) != self._normalized(
            candidate.get("original_text")
        ):
            raise WorkflowConflict("Rewrite paragraph changed after candidate generation.")
        candidate_text = str(candidate.get("candidate_text") or "").strip()
        if not candidate_text:
            raise WorkflowConflict("Rewrite candidate contains no text.")
        candidate_text_sha256 = str(candidate.get("candidate_text_sha256") or "")
        if candidate_text_sha256 and candidate_text_sha256 != self._text_sha256(
            candidate_text
        ):
            raise WorkflowConflict("Rewrite candidate integrity check failed.")
        candidate_evaluation = dict(candidate.get("candidate_evaluation") or {})
        if candidate_evaluation and (
            candidate_evaluation.get("evaluation_scope") != "single_paragraph"
            or str(candidate_evaluation.get("paragraph_id") or "") != paragraph_id
            or str(
                dict(candidate_evaluation.get("paragraph_score") or {}).get(
                    "paragraph_id"
                )
                or ""
            )
            != paragraph_id
        ):
            raise WorkflowConflict("Stored candidate score is invalid.")
        candidate_draft = (
            payload["first_draft_md"][: paragraph["start"]]
            + candidate_text
            + payload["first_draft_md"][paragraph["end"] :]
        ).rstrip() + "\n"
        matching_issues = [
            issue
            for issue in quality.get("issues") or []
            if isinstance(issue, dict) and issue.get("paragraph_id") == paragraph_id
        ]
        allowed_unsupported_claims = sorted(
            {
                str(value)
                for issue in matching_issues
                for value in issue.get("unsupported_claims") or []
                if str(value).strip()
            }
        )
        preflight = quality.get("preflight")
        preflight = preflight if isinstance(preflight, dict) else {}
        paragraph_check = next(
            (
                item
                for item in preflight.get("paragraph_checks") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        case_range = preflight.get("case_word_range")
        if isinstance(case_range, (list, tuple)) and len(case_range) >= 2:
            min_case_words = int(case_range[0] or CASE_PARAGRAPH_MIN_WORDS)
            max_case_words = int(case_range[1] or CASE_PARAGRAPH_MAX_WORDS)
        elif isinstance(case_range, dict):
            min_case_words = int(
                case_range.get("min_words") or CASE_PARAGRAPH_MIN_WORDS
            )
            max_case_words = int(
                case_range.get("max_words") or CASE_PARAGRAPH_MAX_WORDS
            )
        else:
            min_case_words, max_case_words = (
                CASE_PARAGRAPH_MIN_WORDS,
                CASE_PARAGRAPH_MAX_WORDS,
            )
        compatibility = self.compatibility_payload(principal, project_id)
        self.validate_artifact_inputs(principal, project_id, candidate, QUALITY_INPUT_ARTIFACTS)
        source_evidence_id = str(compatibility.get("source_section_evidence_artifact_id") or "")
        source_matrix_id = str(compatibility.get("source_matrix_artifact_id") or "")
        return {
            **compatibility,
            "project_id": project_id,
            "candidate_id": candidate_id,
            "paragraph_id": paragraph_id,
            "paragraph_text": str(paragraph["text"]),
            "candidate_text": candidate_text,
            # New candidates are scored before human review.  The router uses
            # this immutable evaluation directly; the legacy evaluator is only
            # a compatibility fallback for candidates created by older builds.
            "candidate_evaluation": candidate_evaluation,
            "source_paragraph_evaluation": dict(
                candidate.get("source_paragraph_evaluation") or {}
            ),
            "source_section_evidence_artifact_id": source_evidence_id,
            "source_matrix_artifact_id": source_matrix_id,
            "evidence_repair_preview": dict(
                candidate.get("evidence_repair_preview") or {}
            ),
            "claim_dispositions_preview": dict(
                candidate.get("claim_dispositions_preview") or {}
            ),
            "candidate_draft_text": candidate_draft,
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "source_rewrites_artifact_id": store_artifact.id,
            "expected_revision": int(revision),
            "quality": quality,
            "goal": float(quality.get("goal") or quality.get("pass_threshold") or DRAFT_PASS_THRESHOLD),
            "paragraph_goal": float(
                quality.get("paragraph_pass_threshold")
                or quality.get("paragraph_goal")
                or PARAGRAPH_PASS_THRESHOLD
            ),
            "min_case_words": min_case_words,
            "max_case_words": max_case_words,
            "word_range_applicable": bool(
                paragraph_check.get("word_range_applicable", True)
            ),
            "allowed_unsupported_claims": allowed_unsupported_claims,
        }

    def publish_accepted_rewrite(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish one accepted paragraph using its precomputed candidate score."""

        expected_inputs = self.validate_task_inputs(principal, project_id, job_payload)
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        current_quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY
        )
        store, store_artifact = self._read_json(
            principal, project_id, DRAFT_REWRITES
        )
        candidate_id = str(job_payload["candidate_id"])
        entries = dict(store.get("entries") or {})
        candidate = dict(entries.get(candidate_id) or {})
        if not candidate or candidate.get("status") != "pending":
            raise WorkflowConflict("Rewrite candidate was already decided.")
        if str(candidate.get("candidate_text") or "").strip() != str(
            job_payload.get("candidate_text") or ""
        ).strip():
            raise WorkflowConflict("Rewrite candidate changed during evaluation.")
        stored_evaluation = dict(candidate.get("candidate_evaluation") or {})
        self._validate_repair_lineages(principal, candidate.get("evidence_repair_preview") or {})
        if stored_evaluation:
            stored_score = dict(stored_evaluation.get("paragraph_score") or {})
            built_score = dict(built.get("paragraph_score") or {})
            if (
                str(stored_evaluation.get("paragraph_id") or "")
                != str(job_payload["paragraph_id"])
                or stored_score.get("score") != built_score.get("score")
            ):
                raise WorkflowConflict(
                    "The precomputed candidate score changed before saving."
                )
        paragraph_id = str(job_payload["paragraph_id"])
        candidate_draft = str(job_payload.get("candidate_draft_text") or "")
        if not candidate_draft.strip() or candidate_draft == current_text:
            raise WorkflowConflict("Rewrite candidate contains no applicable change.")

        source_evidence_id = str(
            job_payload.get("source_section_evidence_artifact_id") or ""
        )
        source_matrix_id = str(job_payload.get("source_matrix_artifact_id") or "")
        if source_evidence_id:
            repaired_evidence, evidence_repair, claim_dispositions = (
                self._single_paragraph_evidence_repair(
                    job_payload,
                    built,
                    dict(job_payload.get("source_paragraph_evaluation") or {}),
                )
            )
        else:
            repaired_evidence = {}
            evidence_repair = {
                "status": "not_applicable",
                "added_evidence_count": 0,
                "downgraded_claim_count": 0,
                "affected_section_ids": [],
                "affected_paragraph_ids": [],
                "added_evidence": [],
            }
            claim_dispositions = {}

        candidate_matrix, matrix_fact_promotions = self._matrix_with_promoted_facts(
            dict(job_payload.get("matrix") or {}), evidence_repair
        )
        evidence_repair["matrix_fact_promotions"] = matrix_fact_promotions
        evidence_repair["matrix_fact_promotion_count"] = len(matrix_fact_promotions)

        updated_quality = self._incremental_quality(
            current_quality,
            built,
            paragraph_id=paragraph_id,
            source_quality_artifact_id=quality_artifact.id,
        )
        fact_checkpoint = ((built.get("fact_agent_repair") or {}).get("matrix_enrichment_checkpoint")
                           or (stored_evaluation.get("fact_agent_repair") or {}).get("matrix_enrichment_checkpoint"))
        if fact_checkpoint:
            updated_quality.setdefault("feedback_status", {})["fact_repair_checkpoint"] = fact_checkpoint
        merged_dispositions = dict(
            current_quality.get("claim_dispositions") or {}
        )
        merged_dispositions.update(claim_dispositions)
        updated_quality.update(
            {
                "evidence_repair": evidence_repair,
                "claim_dispositions": merged_dispositions,
                "repair_summary": self._repair_summary(
                    current_quality,
                    list(updated_quality.get("root_causes") or []),
                    evidence_repair=evidence_repair,
                ),
            }
        )
        manual_review = self._manual_claim_review(current, updated_quality)
        updated_quality.update(
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
        updated_quality.update(self._quality_status_partition(updated_quality))
        decided_at = utc_now().isoformat()
        candidate.update(
            {
                "status": "accepted",
                "decided_at": decided_at,
                "paragraph_score_before": next(
                    (
                        item.get("score")
                        for item in current_quality.get("paragraph_scores") or []
                        if isinstance(item, dict)
                        and str(item.get("paragraph_id") or "") == paragraph_id
                    ),
                    None,
                ),
                "paragraph_score_after": dict(built.get("paragraph_score") or {}).get(
                    "score"
                ),
                "overall_score_after": updated_quality["score"],
                "evidence_repair": evidence_repair,
                "claim_dispositions": claim_dispositions,
            }
        )
        entries[candidate_id] = candidate
        for other_id, other_value in list(entries.items()):
            if other_id == candidate_id or not isinstance(other_value, dict):
                continue
            other = dict(other_value)
            if (
                other.get("status") == "pending"
                and other.get("source_draft_artifact_id") == current.id
            ):
                other["status"] = "superseded"
                other["superseded_by_candidate_id"] = candidate_id
                other["decided_at"] = decided_at
                entries[other_id] = other

        overlays, overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        overlay_entries = dict(overlays.get("entries") or {})
        previous_overlay = overlay_entries.get(paragraph_id)
        previous_overlay = previous_overlay if isinstance(previous_overlay, dict) else {}
        overlay_entries[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "source_text_sha256": str(
                previous_overlay.get("source_text_sha256")
                or self._text_sha256(str(job_payload["paragraph_text"]))
            ),
            "rewritten_text": str(job_payload["candidate_text"]).strip(),
            "updated_at": decided_at,
        }

        files: dict[
            str,
            tuple[bytes | Callable[[dict[str, ArtifactRecord]], bytes], str],
        ] = {}
        if matrix_fact_promotions:
            files[MATRIX_LOGICAL_NAME] = (
                (
                    json.dumps(candidate_matrix, ensure_ascii=False, indent=2)
                    + "\n"
                ).encode(),
                "json",
            )
        if evidence_repair_has_changes(evidence_repair) and repaired_evidence:
            files[SECTION_EVIDENCE] = (partial(repaired_evidence_content, repaired_evidence), "json")
        files[DRAFT_DOCUMENT] = (
            (candidate_draft.rstrip() + "\n").encode(),
            "markdown",
        )

        def quality_content(published: dict[str, ArtifactRecord]) -> bytes:
            quality_was_current = (
                current_quality.get("source_draft_artifact_id") == current.id
            )
            quality = {
                **updated_quality,
                # A targeted paragraph evaluation cannot make an already stale
                # full-draft evaluation current.  Preserve staleness unless the
                # source quality snapshot covered the complete current draft.
                "source_draft_artifact_id": (
                    published[DRAFT_DOCUMENT].id
                    if quality_was_current
                    else current_quality.get("source_draft_artifact_id")
                ),
            }
            return (json.dumps(quality, ensure_ascii=False, indent=2) + "\n").encode()

        files[DRAFT_QUALITY] = (quality_content, "json")
        files[DRAFT_OVERLAYS] = (
            (
                json.dumps(
                    {
                        **overlays,
                        "schema_version": 1,
                        "project_id": project_id,
                        "policy": "Apply only when paragraph_id and source_text_sha256 still match.",
                        "entries": overlay_entries,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode(),
            "json",
        )
        files[DRAFT_REWRITES] = (
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
        expected_currents = dict(expected_inputs)
        if overlay_artifact is not None:
            expected_currents[DRAFT_OVERLAYS] = overlay_artifact.id
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "rewrite-candidate",
            "subject_id": candidate_id,
            "decision": "accept",
            "details": {
                "paragraph_id": paragraph_id,
                "source_draft_artifact_id": current.id,
                "evaluation_scope": "single_paragraph",
                "paragraph_score_after": candidate.get("paragraph_score_after"),
                "overall_score_after": updated_quality["score"],
                "added_evidence_count": int(
                    evidence_repair.get("added_evidence_count") or 0
                ),
            },
            "created_at": utc_now(),
        }
        common_metadata = {
            **dict(current.metadata),
            "operation": "rewrite-accept-pre-evaluated-candidate",
            "candidate_id": candidate_id,
            "paragraph_id": paragraph_id,
            "previous_draft_artifact_id": current.id,
            "evidence_repair": evidence_repair,
        }

        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=int(job_payload["expected_revision"]),
                metadata=common_metadata,
                metadata_builder=partial(repaired_artifact_metadata, common_metadata),
                approval_events=[event],
                expected_current_artifacts=expected_currents,
                invalidate_final=True,
            )
        return {
            "candidate_id": candidate_id,
            "decision": "accept",
            "draft_artifact_id": published[DRAFT_DOCUMENT].id,
            "quality_artifact_id": published[DRAFT_QUALITY].id,
            "paragraph_id": paragraph_id,
            "paragraph_score": dict(built.get("paragraph_score") or {}).get("score"),
            "score": updated_quality["score"],
            "evaluation_scope": "single_paragraph",
            "section_evidence_artifact_id": (
                published[SECTION_EVIDENCE].id
                if SECTION_EVIDENCE in published
                else source_evidence_id
            ),
            "matrix_artifact_id": (
                published[MATRIX_LOGICAL_NAME].id
                if MATRIX_LOGICAL_NAME in published
                else source_matrix_id
            ),
            "evidence_repair": evidence_repair,
            "revision": state.revision,
        }
