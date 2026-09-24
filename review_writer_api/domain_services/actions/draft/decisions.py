"""Human decisions and approval actions for DraftsService."""

from __future__ import annotations

import json
import uuid
from typing import Any

from review_writer_api.database import utc_now
from review_writer_api.domain_services.actions.draft.errors import DraftApprovalBlocked
from review_writer_api.security import Permission, Principal
from review_writer_core.writing_contracts import substantive_quality_findings
from review_writer_core.workflow.artifacts import (
    DRAFT_APPROVAL,
    DRAFT_MANUSCRIPT as DRAFT_DOCUMENT,
)


class DraftDecisionActionsMixin:
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
