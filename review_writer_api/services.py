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

    def _visible_stage(self, principal: Principal, record: ProjectRecord) -> ProjectRecord:
        """Project navigation follows the saved outline, not Matrix's review flag."""
        states = record.stage_states
        if not states:
            return record
        statuses = {
            stage: str(value.get("status", "pending") if isinstance(value, dict) else value or "pending")
            for stage, value in states.items()
        }
        discovery_done = statuses.get("discovery", "pending").lower() in COMPLETED_STAGE_STATUSES
        outline_ready: bool | None = None
        if discovery_done and self.outline_ready_for_chapter_planning is not None:
            try:
                outline_ready = self.outline_ready_for_chapter_planning(principal, record.project_id)
            except (WorkflowConflict, WorkflowNotFound, OSError, ValueError):
                # A damaged artifact must not break the entire project list.
                outline_ready = False
            # The saved outline, not Matrix's internal approval flag, owns the 03/04 boundary.
            statuses["matrix"] = "approved" if outline_ready else "review"
        completed = record.completed_stages
        if outline_ready is not None:
            completed = tuple(stage for stage in completed if stage != "matrix")
            if outline_ready:
                insert_at = completed.index("discovery") + 1 if "discovery" in completed else 0
                completed = (*completed[:insert_at], "matrix", *completed[insert_at:])
        return replace(record, current_stage=current_user_stage(statuses), completed_stages=completed)

    def list_projects(self, principal: Principal) -> list[ProjectRecord]:
        principal.require(Permission.PROJECT_READ)
        return [self._visible_stage(principal, record) for record in self.repository.list_for_user(principal.user_id)]

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
