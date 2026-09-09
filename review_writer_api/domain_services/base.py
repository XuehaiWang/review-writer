"""Shared service guards for project-scoped domain operations."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import database_session
from review_writer_api.errors import WorkflowConflict, WorkflowNotFound
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_api.workflow_models import LibraryPaper
from review_writer_core.scientific_facts import is_additive_fact_repair
from review_writer_core.workflow.artifacts import MATRIX


class OwnedProjectService:
    """Provide the common read-permission and ownership check for services.

    Concrete domain services assign ``repository`` in their constructors.  Keeping
    this guard in one place prevents small authorization differences from appearing
    as the individual services evolve.
    """

    repository: WorkflowRepository

    @staticmethod
    def _supporting_source_parents(catalog, paper_ids):
        requested = set(paper_ids)
        result = {}
        for source_id, paper in catalog.items():
            metadata = dict(paper.metadata_json or {})
            def value(key):
                raw = metadata.get(key)
                return raw.get("value") if isinstance(raw, dict) else raw
            parent = str(value("parent_paper_id") or "").strip()
            if value("document_type") == "supporting_information" and parent in requested:
                result[source_id] = parent
        return result

    def _catalog(self, principal: Principal, paper_ids: list[str]) -> dict[str, LibraryPaper]:
        """Owned active articles and explicitly linked SI; never title-based guessing."""
        principal.require(Permission.PROJECT_READ)
        with database_session(self.repository.session_factory) as session:
            papers = list(session.scalars(select(LibraryPaper).where(
                LibraryPaper.user_id == uuid.UUID(principal.user_id),
                LibraryPaper.deleted_at.is_(None), LibraryPaper.status == "active",
            )).all())
        catalog = {paper.paper_id: paper for paper in papers}
        allowed = set(paper_ids) | self._supporting_source_parents(catalog, paper_ids).keys()
        return {key: paper for key, paper in catalog.items() if key in allowed}

    def _matrix_dependency_matches(self, principal, project_id, source_id, current_artifact):
        """Compare persisted inputs; display counters/metadata are not lineage."""
        if current_artifact is None or not source_id:
            return False
        if source_id == current_artifact.id:
            return True
        try:
            old = self.artifacts.resolve_owned_artifact(principal.user_id, source_id)
            if old.artifact.project_id != project_id or old.artifact.logical_name != MATRIX:
                return False
            resolved = self.artifacts.resolve_owned_artifact(principal.user_id, current_artifact.id)
            if resolved.artifact.project_id != project_id or resolved.artifact.logical_name != MATRIX:
                return False
            current = json.loads(resolved.path.read_text(encoding="utf-8"))
            return is_additive_fact_repair(json.loads(old.path.read_text(encoding="utf-8")), current)
        except (WorkflowNotFound, WorkflowConflict, OSError, ValueError, TypeError):
            return False

    def _owned_project(self, principal: Principal, project_id: str) -> Any:
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project


class ArtifactBackedService(OwnedProjectService):
    """Share the ordinary current-artifact read path across stage services."""

    artifacts: ArtifactService

    def validate_artifact_inputs(self, principal, project_id, payload, fields):
        """Check recorded input identities, including explicitly absent inputs.

        Return the same expectations for the atomic publication check. Omitted
        keys are legacy inputs; newly built payloads record every dependency.
        """
        self._owned_project(principal, project_id)
        expected = {}
        for field, logical_name in fields.items():
            if field not in payload:
                continue
            source_id = str(payload[field] or "")
            current = self.repository.get_current_artifact(
                principal.user_id, project_id, logical_name
            )
            if (current.id if current else "") != source_id:
                raise WorkflowConflict(
                    "Workflow inputs changed. Start a new task with current inputs.",
                    details={"field": field, "logical_name": logical_name,
                             "expected_artifact_id": source_id,
                             "current_artifact_id": current.id if current else ""},
                )
            expected[logical_name] = source_id
        return expected

    def _artifact(
        self, principal: Principal, project_id: str, logical_name: str
    ) -> ArtifactRecord | None:
        self._owned_project(principal, project_id)
        return self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )

    def _read_text(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[str, ArtifactRecord | None]:
        artifact = self._artifact(principal, project_id, logical_name)
        if artifact is None:
            if required:
                raise WorkflowNotFound("Current workflow artifact not found.")
            return "", None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            return resolved.path.read_text(encoding="utf-8"), artifact
        except OSError as exc:
            raise WorkflowConflict("The current workflow artifact is unreadable.") from exc

    def _read_json(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[dict[str, Any], ArtifactRecord | None]:
        text, artifact = self._read_text(
            principal, project_id, logical_name, required=required
        )
        if artifact is None:
            return {}, None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowConflict("The current workflow artifact is invalid.") from exc
        if not isinstance(value, dict):
            raise WorkflowConflict("The current workflow artifact is invalid.")
        return value, artifact
