"""Application services independent of FastAPI and storage implementation."""

from __future__ import annotations

from .repositories import ProjectRecord, ProjectRepository
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
        return self.repository.create_for_user(
            principal.user_id,
            slug=slug,
            topic=topic,
            taxonomy_profile=taxonomy_profile,
            model_tier=model_tier,
        )

    def update_project_model_tier(
        self, principal: Principal, project_id: str, *, model_tier: str
    ) -> ProjectRecord:
        principal.require(Permission.PROJECT_WRITE)
        return self.repository.update_model_tier_for_user(
            principal.user_id, project_id, model_tier=model_tier
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
