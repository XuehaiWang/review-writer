"""Human decisions and approval actions for DraftsService."""

from __future__ import annotations

import json
import uuid
from typing import Any

from review_writer_api.database import utc_now
from review_writer_api.domain_services.actions.draft.errors import DraftApprovalBlocked
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_core.writing_contracts import substantive_quality_findings
from review_writer_core.workflow.artifacts import (
    DRAFT_APPROVAL,
    DRAFT_MANUSCRIPT as DRAFT_DOCUMENT,
    DRAFT_QUALITY_REPORT as DRAFT_QUALITY,
    DRAFT_REWRITE_CANDIDATES as DRAFT_REWRITES,
    DRAFT_REWRITE_OVERLAYS as DRAFT_OVERLAYS,
)


class DraftDecisionActionsMixin:
    def decide_rewrite(
        self,
        principal: Principal,
        project_id: str,
        candidate_id: str,
        *,
        decision: str,
        revision: int,
    ) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise WorkflowValidationError("Unknown rewrite decision.")
        store, store_artifact = self._read_json(principal, project_id, DRAFT_REWRITES)
        entries = dict(store.get("entries") or {})
        candidate = dict(entries.get(candidate_id) or {})
        if not candidate:
            raise WorkflowNotFound("Rewrite candidate not found.")
        if candidate.get("status") != "pending":
            raise WorkflowConflict("Rewrite candidate was already decided.")
        current_text, current = self._read_text(principal, project_id, DRAFT_DOCUMENT)
        current_quality = self._artifact(principal, project_id, DRAFT_QUALITY)
        overlays, overlay_artifact = self._read_json(
            principal, project_id, DRAFT_OVERLAYS, required=False
        )
        if candidate.get("source_draft_artifact_id") != current.id:
            raise WorkflowConflict("Rewrite candidate is stale for the current Draft.")
        if (
            current_quality is None
            or candidate.get("source_quality_artifact_id") != current_quality.id
        ):
            raise WorkflowConflict("Rewrite candidate is stale for the current evaluation.")
        files: dict[str, tuple[bytes, str]] = {}
        expected_currents = {
            DRAFT_DOCUMENT: current.id,
            DRAFT_QUALITY: current_quality.id,
            DRAFT_REWRITES: store_artifact.id,
        }
        if decision == "accept":
            paragraph = next(
                (
                    row
                    for row in self._paragraph_spans(current_text)
                    if row["paragraph_id"] == candidate.get("paragraph_id")
                ),
                None,
            )
            if paragraph is None or self._normalized(paragraph["text"]) != self._normalized(
                candidate.get("original_text")
            ):
                raise WorkflowConflict("Rewrite paragraph changed after candidate generation.")
            updated = (
                current_text[: paragraph["start"]]
                + str(candidate["candidate_text"]).strip()
                + current_text[paragraph["end"] :]
            )
            files[DRAFT_DOCUMENT] = ((updated.rstrip() + "\n").encode(), "markdown")
            overlay_entries = dict(overlays.get("entries") or {})
            previous_overlay = overlay_entries.get(candidate.get("paragraph_id"))
            previous_overlay = (
                previous_overlay if isinstance(previous_overlay, dict) else {}
            )
            paragraph_id = str(candidate.get("paragraph_id") or "")
            overlay_entries[paragraph_id] = {
                "paragraph_id": paragraph_id,
                "source_text_sha256": str(
                    previous_overlay.get("source_text_sha256")
                    or self._text_sha256(str(candidate.get("original_text") or ""))
                ),
                "rewritten_text": str(candidate["candidate_text"]).strip(),
                "updated_at": utc_now().isoformat(),
            }
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
            if overlay_artifact is not None:
                expected_currents[DRAFT_OVERLAYS] = overlay_artifact.id
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
                    other["decided_at"] = utc_now().isoformat()
                    entries[other_id] = other
        candidate["status"] = "accepted" if decision == "accept" else "rejected"
        candidate["decided_at"] = utc_now().isoformat()
        entries[candidate_id] = candidate
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
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "rewrite-candidate",
            "subject_id": candidate_id,
            "decision": decision,
            "details": {
                "paragraph_id": candidate.get("paragraph_id"),
                "source_draft_artifact_id": current.id,
            },
            "created_at": utc_now(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=revision,
                metadata={
                    **dict(current.metadata),
                    "operation": f"rewrite-{decision}",
                    "candidate_id": candidate_id,
                    "previous_draft_artifact_id": current.id,
                },
                approval_events=[event],
                expected_current_artifacts=expected_currents,
                invalidate_final=decision == "accept",
            )
        return {
            "candidate_id": candidate_id,
            "decision": decision,
            "draft_artifact_id": (
                published[DRAFT_DOCUMENT].id if DRAFT_DOCUMENT in published else current.id
            ),
            "revision": state.revision,
        }

    def approve(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        override_low_score: bool,
        override_reason: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        payload = self.get(principal, project_id)
        if not payload.get("draft_artifact_id") or payload.get("freshness", {}).get("upstream_stale"):
            raise DraftApprovalBlocked("A current saved Draft is required for approval.")
        quality = (payload.get("quality") or {}) if payload.get("quality", {}).get("current") else {}
        hard = [str(value) for value in quality.get("hard_gate_failures") or [] if str(value)]
        # Approval authorizes workflow progression, not scientific verification.
        # Keep the exact Quality artifact and its findings unchanged for audit.
        findings = substantive_quality_findings(quality)
        draft_id = str(payload["draft_artifact_id"])
        quality_artifact_id = str(payload.get("quality_artifact_id") or "")
        approval = {
            "status": "approved",
            "draft_artifact_id": draft_id,
            "quality_artifact_id": quality_artifact_id,
            "approval_mode": "acknowledged_findings" if hard or findings or quality.get("release_integrity_failure") else "standard",
            "overridden_hard_gate_failures": hard,
            "acknowledged_findings": findings,
            "acknowledged_release_integrity_failure": bool(quality.get("release_integrity_failure")),
            "acknowledged_release_integrity_failures": list(quality.get("release_integrity_failures") or []),
            "override_reason": str(override_reason or "").strip(),
            "approved_at": utc_now().isoformat(),
        }
        event = {
            "id": str(uuid.uuid4()),
            "stage_id": "draft",
            "subject_type": "draft-version",
            "subject_id": draft_id,
            "decision": "approved",
            "details": approval,
            "created_at": utc_now(),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    DRAFT_APPROVAL: (
                        (json.dumps(approval, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                status="approved",
                metadata={"operation": "draft-approval", "draft_artifact_id": draft_id},
                approval_events=[event],
                expected_current_artifacts={
                    DRAFT_DOCUMENT: draft_id,
                },
                invalidate_final=False,
            )
        return {
            "approved": True,
            "approval_artifact_id": published[DRAFT_APPROVAL].id,
            "revision": state.revision,
            "next_stage": "final",
        }
