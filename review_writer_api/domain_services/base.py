"""Shared service guards for project-scoped domain operations."""

from __future__ import annotations

from typing import Any

from review_writer_api.errors import WorkflowNotFound
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import WorkflowRepository


class OwnedProjectService:
    """Provide the common read-permission and ownership check for services.

    Concrete domain services assign ``repository`` in their constructors.  Keeping
    this guard in one place prevents small authorization differences from appearing
    as the individual services evolve.
    """

    repository: WorkflowRepository

    def _owned_project(self, principal: Principal, project_id: str) -> Any:
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project
