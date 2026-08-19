"""Application services independent of FastAPI and storage implementation."""

from __future__ import annotations

from review_writer_core.taxonomy import TaxonomyConfigurationError, validate_taxonomy_profile

from .repositories import (
    ProjectRecord,
    ProjectRepository,
    ProjectTaxonomyUpdateResult,
)
from .security import Permission, Principal


class ProjectService:
    def __init__(self, repository: ProjectRepository):
        self.repository = repository

    def list_projects(self, principal: Principal) -> list[ProjectRecord]:
        principal.require(Permission.PROJECT_READ)
        return self.repository.list_for_user(principal.user_id)

    def get_project(self, principal: Principal, project_id: str) -> ProjectRecord | None:
        principal.require(Permission.PROJECT_READ)
        return self.repository.get_for_user(principal.user_id, project_id)

    def create_project(
        self,
        principal: Principal,
        *,
        slug: str,
        topic: str,
        taxonomy_profile: str,
        model_tier: str = "terra",
    ) -> ProjectRecord:
        principal.require(Permission.PROJECT_WRITE)
        try:
            selected_profile = validate_taxonomy_profile(taxonomy_profile)
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
            selected_profile = validate_taxonomy_profile(taxonomy_profile)
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

    def update_project_topic(
        self,
        principal: Principal,
        project_id: str,
        *,
        topic: str,
        taxonomy_profile: str,
    ) -> ProjectRecord:
        principal.require(Permission.PROJECT_WRITE)
        return self.repository.update_topic_for_user(
            principal.user_id,
            project_id,
            topic=topic,
            taxonomy_profile=taxonomy_profile,
        )

    def sync_stage_states(
        self,
        principal: Principal,
        project_id: str,
        stage_states: dict[str, object],
        current_stage: str,
    ) -> None:
        principal.require(Permission.PROJECT_WRITE)
        self.repository.sync_stage_states_for_user(
            principal.user_id,
            project_id,
            stage_states,
            current_stage,
        )
