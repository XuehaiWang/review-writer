"""Quality evaluation inputs and publication actions for DraftsService."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from review_writer_api.database import database_session, utc_now
from review_writer_api.domain_services.actions.draft.errors import DraftNotReady
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.security import Principal
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord
from review_writer_core.draft_bibliography import citation_entries_from_draft
from review_writer_core.draft_issue_routing import draft_fact_repair_checkpoint
from review_writer_core.draft_quality import (
    QUALITY_INPUT_ARTIFACTS,
    full_draft_quality_provenance,
    quality_input_artifact_ids,
    reusable_full_draft_quality,
)
from review_writer_core.paragraph_markers import ensure_prose_paragraph_markers
from review_writer_core.writing_contracts import (
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    DRAFT_PASS_THRESHOLD,
    PARAGRAPH_PASS_THRESHOLD,
)
from review_writer_core.workflow.artifacts import (
    BLUEPRINT as BLUEPRINT_LOGICAL_NAME,
    DRAFT_MANUSCRIPT as DRAFT_DOCUMENT,
    DRAFT_QUALITY_REPORT as DRAFT_QUALITY,
    DRAFT_REWRITE_CANDIDATES,
    DRAFT_REWRITE_OVERLAYS as DRAFT_OVERLAYS,
    FIGURE_MANIFEST,
    MATRIX as MATRIX_LOGICAL_NAME,
    SECTION_DRAFTS as SECTION_INDEX,
    SECTION_EVIDENCE_PACKAGE as SECTION_EVIDENCE,
    SECTION_WRITING_PLAN,
)


class DraftQualityActionsMixin:
    def validate_task_inputs(self, principal, project_id, payload):
        """Reuse the evaluation dependency contract for all Draft jobs."""
        if payload.get("revision_mode") == "dialogue":
            return self.validate_dialogue_inputs(principal, project_id, payload)
        expected = self.validate_artifact_inputs(principal, project_id, payload, {
            **QUALITY_INPUT_ARTIFACTS,
            "source_draft_artifact_id": DRAFT_DOCUMENT,
            "source_quality_artifact_id": DRAFT_QUALITY,
            "source_rewrites_artifact_id": DRAFT_REWRITE_CANDIDATES,
        })
        state = self.repository.get_stage_state(principal.user_id, project_id, "draft")
        if state is not None and state.status == "stale":
            raise WorkflowConflict("Draft inputs changed. Reassemble Draft before running this task.")
        if ("expected_revision" in payload and not payload.get("source_draft_artifact_id")
                and (state.revision if state else 0) != payload["expected_revision"]):
            raise WorkflowConflict("Draft changed while the task was waiting or running.")
        return expected

    def evaluation_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        goal: float,
        paragraph_goal: float = PARAGRAPH_PASS_THRESHOLD,
        max_iterations: int = 2,
        min_case_words: int = CASE_PARAGRAPH_MIN_WORDS,
        max_case_words: int = CASE_PARAGRAPH_MAX_WORDS,
    ) -> dict[str, Any]:
        payload = self.get(principal, project_id)
        if not payload["draft_artifact_id"]:
            raise DraftNotReady("Assemble and save Draft before evaluation.")
        if payload["freshness"]["upstream_stale"]:
            raise DraftNotReady("Draft inputs changed. Reassemble Draft before evaluation.")
        marked_text, marker_report = ensure_prose_paragraph_markers(
            payload["first_draft_md"]
        )
        if int(marker_report.get("prose_paragraph_count") or 0) < 1:
            raise DraftNotReady("The current Draft contains no prose paragraphs to evaluate.")
        if marker_report.get("changed"):
            self.save_text(
                principal,
                project_id,
                text=marked_text,
                revision=int(payload["revision"]),
                operation="evaluation-marker-normalization",
            )
            payload = self.get(principal, project_id)
        safe_min_words = max(1, int(min_case_words))
        safe_max_words = max(1, int(max_case_words))
        if safe_max_words < safe_min_words:
            raise WorkflowValidationError(
                "The maximum case word count must not be lower than the minimum."
            )
        compatibility = self.compatibility_payload(principal, project_id)
        current_quality = (
            dict(payload.get("quality") or {})
            if bool((payload.get("quality") or {}).get("current"))
            else {}
        )
        quality_reusable, quality_reuse_reasons = reusable_full_draft_quality(
            current_quality,
            draft_artifact_id=str(payload["draft_artifact_id"]),
            draft_text=str(payload["first_draft_md"]),
            paragraphs=payload["paragraphs"],
            input_artifact_ids=quality_input_artifact_ids(compatibility),
        )
        return {
            **compatibility,
            "project_id": project_id,
            "source_draft_artifact_id": payload["draft_artifact_id"],
            "source_quality_artifact_id": payload["quality_artifact_id"],
            "expected_revision": payload["revision"],
            "draft_text": payload["first_draft_md"],
            "paragraphs": payload["paragraphs"],
            "issues": list(current_quality.get("issues") or []),
            "baseline_quality": current_quality if quality_reusable else {},
            "quality_reused": quality_reusable,
            "quality_reuse_reasons": quality_reuse_reasons,
            "run_mode": "steady_state" if quality_reusable else "migration",
            "goal": max(0.0, min(float(goal), 100.0)),
            "paragraph_goal": max(0.0, min(float(paragraph_goal), 100.0)),
            "max_iterations": max(1, min(int(max_iterations), 10)),
            "min_case_words": safe_min_words,
            "max_case_words": safe_max_words,
            "citation_identity": citation_entries_from_draft(
                payload["first_draft_md"],
                dict(compatibility.get("section_index") or {}),
            ),
            "prior_quality_context": {
                "fact_repair_checkpoint": draft_fact_repair_checkpoint(payload),
                "evidence_rescue_cache": dict((current_quality.get("feedback_status") or {}).get("evidence_rescue_cache") or {}),
                "repair_history": {
                    **dict((current_quality.get("feedback_status") or {}).get("repair_history") or {}),
                    **{key: value for proposal in payload.get("optimization_proposals") or []
                       if proposal.get("source_draft_artifact_id") == payload["draft_artifact_id"]
                       for key, value in ((proposal.get("feedback_status") or {}).get("repair_history") or {}).items()},
                },
                "source_quality_artifact_id": payload["quality_artifact_id"],
                "claim_dispositions": dict(
                    current_quality.get("claim_dispositions") or {}
                ),
                "manual_issue_fingerprints": [
                    str(issue.get("issue_fingerprint") or "")
                    for issue in current_quality.get("issues") or []
                    if isinstance(issue, dict)
                    and not bool(issue.get("auto_repairable", True))
                    and str(issue.get("issue_fingerprint") or "")
                ],
                "unverified_manual_paragraph_ids": list(
                    current_quality.get("unverified_manual_paragraph_ids") or []
                ),
            },
        }

    def compatibility_payload(
        self, principal: Principal, project_id: str
    ) -> dict[str, Any]:
        project = self.repository.get_owned_project(principal.user_id, project_id)
        matrix, matrix_artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME, required=False
        )
        sections, sections_artifact = self._read_json(
            principal, project_id, SECTION_INDEX, required=False
        )
        section_evidence, section_evidence_artifact = self._read_json(
            principal, project_id, SECTION_EVIDENCE, required=False
        )
        figures, figures_artifact = self._read_json(
            principal, project_id, FIGURE_MANIFEST, required=False
        )
        blueprint, blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        writing_plan, writing_plan_artifact = self._read_json(
            principal, project_id, SECTION_WRITING_PLAN, required=False
        )
        overlays, overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        saved_quality, _ = self._read_json(principal, project_id, DRAFT_QUALITY, required=False)
        # Stage confirmation adopts the available prose. Project a read-only
        # writing scope so later assessment cannot demand the omitted chapters
        # again. The original Blueprint and section artifacts stay immutable.
        section_state = self.repository.get_stage_state(principal.user_id, project_id, "sections")
        if (section_state and section_state.status == "approved" and blueprint_artifact
                and sections.get("source_blueprint_artifact_id") == blueprint_artifact.id):
            deferred = [s["section_id"] for s in sections.get("sections") or []
                        if s.get("generation_mode") == "pending_evidence"]
            if deferred or any(s.get("organizing_only") for s in blueprint.get("sections") or []):
                blueprint = {**blueprint, "deferred_section_ids": deferred,
                             "sections": [s for s in blueprint.get("sections") or []
                                          if s.get("section_id") not in deferred and not s.get("organizing_only")]}
                sections = {**sections, "sections": [s for s in sections.get("sections") or []
                                                     if s.get("section_id") not in deferred]}
        artifact_paths: dict[str, str] = {}
        for row in figures.get("figures") or []:
            if not isinstance(row, dict):
                continue
            artifact_id = str(row.get("output_artifact_id") or "")
            if artifact_id:
                artifact_paths[artifact_id] = str(
                    self.artifacts.resolve_owned_artifact(
                        principal.user_id, artifact_id
                    ).path
                )
        paper_ids = {
            str(row.get("paper_id") or "")
            for row in matrix.get("rows") or []
            if isinstance(row, dict) and row.get("paper_id")
        }
        library_metadata: dict[str, dict[str, Any]] = {}
        if paper_ids:
            with database_session(self.repository.session_factory) as session:
                rows = session.scalars(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == uuid.UUID(principal.user_id),
                        LibraryPaper.paper_id.in_(tuple(paper_ids)),
                        LibraryPaper.deleted_at.is_(None),
                    )
                ).all()
                library_metadata = {
                    row.paper_id: dict(row.metadata_json or {}) for row in rows
                }
        from review_writer_core.stages.draft.revisions import effective_writing_plan
        try:
            effective_plan = effective_writing_plan(writing_plan, overlays)
        except ValueError as exc:
            raise WorkflowConflict(str(exc)) from exc
        return {
            "draft_source_check": dict(saved_quality.get("source_check") or {}),
            "baseline_writing_plan": writing_plan,
            "matrix": matrix,
            "blueprint": blueprint,
            "section_index": sections,
            "section_evidence": section_evidence,
            "writing_plan": effective_plan,
            "figure_manifest": figures,
            "figure_artifact_paths": artifact_paths,
            "library_metadata": library_metadata,
            "rewrite_overlays": overlays,
            "taxonomy_profile": str(
                project.taxonomy_profile if project is not None else "general_academic"
            ),
            "source_matrix_artifact_id": matrix_artifact.id if matrix_artifact else "",
            "source_blueprint_artifact_id": blueprint_artifact.id if blueprint_artifact else "",
            "source_figure_manifest_artifact_id": figures_artifact.id if figures_artifact else "",
            "source_sections_artifact_id": sections_artifact.id if sections_artifact else "",
            "source_section_evidence_artifact_id": (
                section_evidence_artifact.id if section_evidence_artifact else ""
            ),
            "source_writing_plan_artifact_id": (
                writing_plan_artifact.id if writing_plan_artifact else ""
            ),
            "source_rewrite_overlay_artifact_id": (
                overlay_artifact.id if overlay_artifact else ""
            ),
        }

    def automatic_synthesis_source(
        self,
        principal: Principal,
        project_id: str,
        *,
        text: str | None = None,
        draft: ArtifactRecord | None = None,
    ) -> dict[str, Any]:
        """Return only source-verified Draft prose for automatic downstream synthesis."""

        if text is None or draft is None:
            text, draft = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        quality, quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        excluded = {
            str(value)
            for value in quality.get("unverified_manual_paragraph_ids") or []
            if str(value).strip()
        }
        if (
            quality_artifact is None
            or quality.get("source_draft_artifact_id") != draft.id
        ):
            excluded = set()
        filtered = str(text)
        removed: list[str] = []
        for paragraph in reversed(self._paragraph_spans(filtered)):
            paragraph_id = str(paragraph["paragraph_id"])
            if paragraph_id not in excluded:
                continue
            filtered = (
                filtered[: int(paragraph["start"])]
                + "\n"
                + filtered[int(paragraph["marker_end"]) :]
            )
            removed.append(paragraph_id)
        return {
            "draft_text": filtered.rstrip() + "\n",
            "source_draft_artifact_id": draft.id,
            "source_quality_artifact_id": quality_artifact.id if quality_artifact else "",
            "excluded_manual_paragraph_ids": sorted(removed),
            "warning_required": bool(removed),
        }

    def publish_evaluation(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        expected_inputs = self.validate_task_inputs(principal, project_id, job_payload)
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        prior_quality, _prior_quality_artifact = self._read_json(
            principal, project_id, DRAFT_QUALITY, required=False
        )
        score = max(0.0, min(float(built.get("score") or 0), 100.0))
        routed_issues, routing = self._quality_routing(built, job_payload)
        root_causes, repair_tasks = self._quality_root_causes(routed_issues)
        manual_review = self._manual_claim_review(current, built)
        claim_dispositions = dict(prior_quality.get("claim_dispositions") or {})
        quality = {
            **{key: value for key, value in built.items() if key != "source_draft_artifact_id"},
            "issues": routed_issues,
            "routing": routing,
            "root_causes": root_causes,
            "repair_tasks": repair_tasks,
            "repair_summary": self._repair_summary(prior_quality, root_causes),
            "claim_dispositions": claim_dispositions,
            "manual_claim_review": manual_review,
            "verified_manual_paragraph_ids": manual_review[
                "verified_manual_paragraph_ids"
            ],
            "unverified_manual_paragraph_ids": manual_review[
                "unverified_manual_paragraph_ids"
            ],
            "source_draft_artifact_id": current.id,
            "score": score,
            "goal": float(built.get("goal") or job_payload.get("goal") or DRAFT_PASS_THRESHOLD),
            "status": "completed",
            "evaluated_at": utc_now().isoformat(),
            **full_draft_quality_provenance(
                current_text,
                self._paragraph_spans(current_text),
                input_artifact_ids=quality_input_artifact_ids(job_payload),
            ),
        }
        quality.update(self._quality_status_partition(quality))
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_QUALITY: (
                        (json.dumps(quality, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=self._revision(principal, project_id),
                metadata={"source_draft_artifact_id": current.id, "operation": "evaluate"},
                expected_current_artifacts=expected_inputs,
                invalidate_final=False,
            )
        return {
            "quality_artifact_id": published[DRAFT_QUALITY].id,
            "score": score,
            "feedback_status": (
                dict(built.get("feedback_status"))
                if isinstance(built.get("feedback_status"), dict)
                else {}
            ),
            "repair_tasks": repair_tasks,
            "repair_status": str(
                (quality.get("repair_summary") or {}).get("repair_status")
                or "not_started"
            ),
            "revision": state.revision,
        }
