"""Application services independent of FastAPI and storage implementation."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from review_writer_core.taxonomy import (
    TaxonomyConfigurationError,
    validate_selectable_taxonomy_profile,
)

from .repositories import (
    ProjectRecord,
    ProjectRepository,
    ProjectTaxonomyUpdateResult,
)
from .security import Permission, Principal
from .errors import WorkflowConflict, WorkflowNotFound
from .workflow_contracts import COMPLETED_STAGE_STATUSES, current_user_stage


class ProjectService:
    def __init__(self, repository: ProjectRepository):
        self.repository = repository
        self.outline_ready_for_chapter_planning: Callable[[Principal, str], bool] | None = None

    def _visible_stage(self, principal: Principal, record: ProjectRecord, *,
                       boundary: dict[str, str] | None = None) -> ProjectRecord:
        """Project navigation follows the saved outline, not Matrix's review flag.

        ``boundary`` carries the outline facts a list already read in bulk, so a
        workspace with many projects does not open one outline file per card.
        It is only trusted when it answers the question definitively; otherwise
        the full check runs for this project alone.
        """
        states = record.stage_states
        if not states:
            return record
        statuses = {
            stage: str(value.get("status", "pending") if isinstance(value, dict) else value or "pending")
            for stage, value in states.items()
        }
        discovery_done = statuses.get("discovery", "pending").lower() in COMPLETED_STAGE_STATUSES
        outline_ready: bool | None = None
        outline_state = "unchecked"
        cheap = self._outline_ready_from_boundary(record, boundary) if discovery_done else None
        if cheap is not None:
            outline_ready = cheap
            outline_state = "ready" if cheap else "not_ready"
            statuses["matrix"] = "approved" if cheap else "review"
        elif discovery_done and self.outline_ready_for_chapter_planning is not None:
            try:
                outline_ready = self.outline_ready_for_chapter_planning(principal, record.project_id)
            except (WorkflowConflict, WorkflowNotFound, OSError, ValueError):
                # A damaged artifact is reported as an unreadable outline, never
                # as an ordinary "not done yet" step. Recorded stage facts stand,
                # so the reader is not silently moved backwards.
                outline_ready = None
                outline_state = "unknown"
            else:
                outline_state = "ready" if outline_ready else "not_ready"
                # The saved outline, not Matrix's internal approval flag, owns the 03/04 boundary.
                statuses["matrix"] = "approved" if outline_ready else "review"
        # Completion facts only: whether the outline owns the 03/04 boundary is an
        # I/O-derived business fact. Naming checkpoints for the reader happens
        # once, in workflow_contracts.public_stage_view, at the response boundary.
        completed = record.completed_stages
        if outline_ready is not None:
            completed = tuple(stage for stage in completed if stage != "matrix")
            if outline_ready:
                insert_at = completed.index("discovery") + 1 if "discovery" in completed else 0
                completed = (*completed[:insert_at], "matrix", *completed[insert_at:])
        return replace(record, current_stage=current_user_stage(statuses), completed_stages=completed,
                       outline_state=outline_state)

    @staticmethod
    def _outline_ready_from_boundary(
        record: ProjectRecord, boundary: dict[str, str] | None
    ) -> bool | None:
        """Answer the 03/04 boundary from pre-read artifact facts, when possible."""
        if not boundary:
            return None
        matrix_state = record.stage_states.get("matrix")
        matrix_status = str((matrix_state or {}).get("status") or "pending")
        if matrix_status in {"pending", "stale", "failed"}:
            # Matrix is not usable, so the outline cannot own the boundary.
            return False
        if not boundary.get("outline_artifact_id"):
            return False
        if "outline_complete" not in boundary:
            # A legacy outline carries no facts: never guess, run the full check.
            return None
        if str(boundary.get("outline_complete")) != "true" or str(boundary.get("outline_has_markdown")) != "true":
            return False
        # The outline is current only against the matrix it was built from. Any
        # other case (compatible re-enrichment, dependency match) needs the full
        # check, so return None and let the caller fall back.
        if boundary.get("outline_source_matrix_artifact_id") == boundary.get("matrix_artifact_id"):
            return True
        return None

    def list_projects(self, principal: Principal) -> list[ProjectRecord]:
        principal.require(Permission.PROJECT_READ)
        records = self.repository.list_for_user(principal.user_id)
        read_boundary = getattr(self.repository, "outline_boundary_inputs", None)
        boundaries = (read_boundary(principal.user_id, [r.project_id for r in records])
                      if read_boundary is not None else {})
        return [self._visible_stage(principal, record, boundary=boundaries.get(record.project_id))
                for record in records]

    def get_project(self, principal: Principal, project_id: str) -> ProjectRecord | None:
        principal.require(Permission.PROJECT_READ)
        record = self.repository.get_for_user(principal.user_id, project_id)
        return self._visible_stage(principal, record) if record else None

    def create_project(
        self,
        principal: Principal,
        *,
        slug: str,
        topic: str,
        taxonomy_profile: str,
        model_tier: str | None = None,
    ) -> ProjectRecord:
        principal.require(Permission.PROJECT_WRITE)
        try:
            selected_profile = validate_selectable_taxonomy_profile(taxonomy_profile)
        except TaxonomyConfigurationError as exc:
            from .repositories import ProjectOperationError

            raise ProjectOperationError(str(exc)) from exc
        return self.repository.create_for_user(
            principal.user_id,
            slug=slug,
            topic=topic,
            taxonomy_profile=selected_profile,
            model_tier=model_tier,
        )

    def update_project_model_tier(
        self, principal: Principal, project_id: str, *, model_tier: str
    ) -> ProjectRecord:
        principal.require(Permission.PROJECT_WRITE)
        return self.repository.update_model_tier_for_user(
            principal.user_id, project_id, model_tier=model_tier
        )

    def update_project_taxonomy_profile(
        self,
        principal: Principal,
        project_id: str,
        *,
        taxonomy_profile: str,
        confirm_downstream_invalidation: bool = False,
    ) -> ProjectTaxonomyUpdateResult:
        principal.require(Permission.PROJECT_WRITE)
        try:
            selected_profile = validate_selectable_taxonomy_profile(taxonomy_profile)
        except TaxonomyConfigurationError as exc:
            from .repositories import ProjectOperationError

            raise ProjectOperationError(str(exc)) from exc
        return self.repository.update_taxonomy_profile_for_user(
            principal.user_id,
            project_id,
            taxonomy_profile=selected_profile,
            confirm_downstream_invalidation=confirm_downstream_invalidation,
        )

    def delete_project(self, principal: Principal, project_id: str) -> bool:
        principal.require(Permission.PROJECT_DELETE)
        return self.repository.delete_for_user(principal.user_id, project_id)

    def restore_project(self, principal: Principal, project_id: str) -> bool:
        principal.require(Permission.PROJECT_DELETE)
        return self.repository.restore_for_user(principal.user_id, project_id)
