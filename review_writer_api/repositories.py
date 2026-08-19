"""Project repository contracts with local and user-isolated hosted adapters."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from review_writer_core.project_catalog import list_review_projects, project_summary
from review_writer_core.project_config import save_project_config
from review_writer_core.taxonomy import DEFAULT_TAXONOMY_PROFILE, validate_taxonomy_profile
from review_writer_core.workspace import WorkspaceConfigurationError, WorkspacePaths, validate_project_id

from .database import Project, database_session, utc_now
from .model_catalog import DEFAULT_MODEL_TIER, resolve_model_tier
from .workflow_models import WorkflowStageState


class ProjectOperationError(ValueError):
    pass


PRIMARY_WORKFLOW_STAGES = (
    "discovery",
    "matrix",
    "blueprint",
    "sections",
    "figure-review",
    "figures",
    "draft",
    "final",
)
COMPLETED_STAGE_STATUSES = {"approved", "completed", "success", "succeeded"}


def current_stage_from_states(stage_states: dict[str, object]) -> str:
    """Return the first primary stage that still needs work or human review."""

    for stage_id in PRIMARY_WORKFLOW_STAGES:
        value = stage_states.get(stage_id)
        status = str(value.get("status") if isinstance(value, dict) else value or "").casefold()
        if status not in COMPLETED_STAGE_STATUSES:
            return stage_id
    return PRIMARY_WORKFLOW_STAGES[-1]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    slug: str
    owner_user_id: str
    topic: str
    discovery_status: str
    completed_stages: tuple[str, ...]
    taxonomy_profile: str = DEFAULT_TAXONOMY_PROFILE
    model_tier: str = DEFAULT_MODEL_TIER
    current_stage: str = "discovery"
    stage_states: dict[str, object] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ProjectTaxonomyUpdateResult:
    project: ProjectRecord
    changed: bool
    matrix_entered: bool
    downstream_stale: bool


class ProjectRepository(Protocol):
    def list_for_user(self, user_id: str) -> list[ProjectRecord]: ...

    def get_for_user(self, user_id: str, project_id: str) -> ProjectRecord | None: ...

    def create_for_user(
        self, user_id: str, *, slug: str, topic: str, taxonomy_profile: str,
        model_tier: str = DEFAULT_MODEL_TIER,
    ) -> ProjectRecord: ...

    def update_model_tier_for_user(
        self, user_id: str, project_id: str, *, model_tier: str
    ) -> ProjectRecord: ...

    def update_taxonomy_profile_for_user(
        self,
        user_id: str,
        project_id: str,
        *,
        taxonomy_profile: str,
        confirm_downstream_invalidation: bool = False,
    ) -> ProjectTaxonomyUpdateResult: ...

    def update_topic_for_user(
        self, user_id: str, project_id: str, *, topic: str, taxonomy_profile: str
    ) -> ProjectRecord: ...

    def delete_for_user(self, user_id: str, project_id: str) -> bool: ...

    def restore_for_user(self, user_id: str, project_id: str) -> bool: ...

    def sync_stage_states_for_user(
        self,
        user_id: str,
        project_id: str,
        stage_states: dict[str, object],
        current_stage: str,
    ) -> None: ...


class LocalProjectRepository:
    """Read existing project folders for the single-user local edition."""

    _STAGE_FLAGS = {
        "discovery": "has_discovery",
        "matrix": "has_matrix_outline",
        "blueprint": "has_blueprint",
        "sections": "has_section_drafting",
        "figures": "has_figure_redraw",
        "draft": "has_first_draft",
        "final": "has_final_audit",
    }

    def __init__(self, review_root: Path, *, user_id: str):
        self.review_root = Path(review_root).resolve()
        self.user_id = user_id

    def _record(self, payload: dict[str, object]) -> ProjectRecord:
        stage_states = {
            stage: {"status": "completed"}
            for stage, flag in self._STAGE_FLAGS.items()
            if bool(payload.get(flag))
        }
        if bool(payload.get("has_figure_redraw")):
            stage_states["figure-review"] = {"status": "completed"}
        return ProjectRecord(
            project_id=str(payload["project_id"]),
            slug=str(payload["project_id"]),
            owner_user_id=self.user_id,
            topic=str(payload.get("topic") or ""),
            discovery_status=str(payload.get("discovery_status") or "pending"),
            completed_stages=tuple(
                stage_id
                for stage_id in PRIMARY_WORKFLOW_STAGES
                if stage_id in stage_states
            ),
            taxonomy_profile=str(payload.get("taxonomy_profile") or "chemistry_general"),
            model_tier=DEFAULT_MODEL_TIER,
            current_stage=current_stage_from_states(stage_states),
            stage_states=stage_states,
        )

    def list_for_user(self, user_id: str) -> list[ProjectRecord]:
        if user_id != self.user_id:
            return []
        return [self._record(payload) for payload in list_review_projects(self.review_root)]

    def get_for_user(self, user_id: str, project_id: str) -> ProjectRecord | None:
        if user_id != self.user_id:
            return None
        try:
            safe_project_id = validate_project_id(project_id)
        except WorkspaceConfigurationError:
            return None
        payload = project_summary(self.review_root, safe_project_id)
        return self._record(payload) if payload else None

    def create_for_user(
        self, user_id: str, *, slug: str, topic: str, taxonomy_profile: str,
        model_tier: str = DEFAULT_MODEL_TIER,
    ) -> ProjectRecord:
        if user_id != self.user_id:
            raise ProjectOperationError("Project owner does not match the local workspace user.")
        try:
            safe_slug = validate_project_id(slug)
        except WorkspaceConfigurationError as exc:
            raise ProjectOperationError(str(exc)) from exc
        paths = WorkspacePaths(self.review_root)
        project = paths.project(safe_slug)
        if project.exists():
            raise ProjectOperationError("A project with this ID already exists.")
        project.mkdir(parents=True)
        save_project_config(
            self.review_root,
            safe_slug,
            topic=str(topic or "").strip(),
            taxonomy_profile=validate_taxonomy_profile(
                taxonomy_profile or DEFAULT_TAXONOMY_PROFILE
            ),
        )
        payload = project_summary(self.review_root, safe_slug)
        if payload is None:
            raise ProjectOperationError("The local project could not be created.")
        return self._record(payload)

    def update_model_tier_for_user(
        self, user_id: str, project_id: str, *, model_tier: str
    ) -> ProjectRecord:
        selected_tier = resolve_model_tier(model_tier).id
        record = self.get_for_user(user_id, project_id)
        if record is None:
            raise ProjectOperationError("Project not found.")
        return ProjectRecord(**{**record.__dict__, "model_tier": selected_tier})

    def update_taxonomy_profile_for_user(
        self,
        user_id: str,
        project_id: str,
        *,
        taxonomy_profile: str,
        confirm_downstream_invalidation: bool = False,
    ) -> ProjectTaxonomyUpdateResult:
        selected_profile = validate_taxonomy_profile(taxonomy_profile)
        record = self.get_for_user(user_id, project_id)
        if record is None:
            raise ProjectOperationError("Project not found.")
        matrix_entered = "matrix" in record.stage_states
        changed = record.taxonomy_profile != selected_profile
        if changed and matrix_entered and not confirm_downstream_invalidation:
            raise ProjectOperationError(
                "Changing the taxonomy profile after Matrix entry requires "
                "confirm_downstream_invalidation=true."
            )
        if changed:
            save_project_config(
                self.review_root,
                record.project_id,
                topic=record.topic,
                taxonomy_profile=selected_profile,
                updates={"retrieval_configuration_status": "stale"},
            )
            record = self.get_for_user(user_id, project_id)
            if record is None:
                raise ProjectOperationError("Project not found.")
        return ProjectTaxonomyUpdateResult(
            project=record,
            changed=changed,
            matrix_entered=matrix_entered,
            downstream_stale=bool(changed and matrix_entered),
        )

    def delete_for_user(self, user_id: str, project_id: str) -> bool:
        raise ProjectOperationError("Delete the local project from the workflow dashboard.")

    def restore_for_user(self, user_id: str, project_id: str) -> bool:
        return self.get_for_user(user_id, project_id) is not None

    def update_topic_for_user(
        self, user_id: str, project_id: str, *, topic: str, taxonomy_profile: str
    ) -> ProjectRecord:
        if user_id != self.user_id:
            raise ProjectOperationError("Project owner does not match the local workspace user.")
        try:
            safe_project_id = validate_project_id(project_id)
        except WorkspaceConfigurationError as exc:
            raise ProjectOperationError(str(exc)) from exc
        if not WorkspacePaths(self.review_root).project(safe_project_id).is_dir():
            raise ProjectOperationError("Project not found.")
        save_project_config(
            self.review_root,
            safe_project_id,
            topic=str(topic or "").strip(),
            taxonomy_profile=validate_taxonomy_profile(
                taxonomy_profile or DEFAULT_TAXONOMY_PROFILE
            ),
        )
        payload = project_summary(self.review_root, safe_project_id)
        if payload is None:
            raise ProjectOperationError("Project not found.")
        return self._record(payload)

    def sync_stage_states_for_user(
        self,
        user_id: str,
        project_id: str,
        stage_states: dict[str, object],
        current_stage: str,
    ) -> None:
        return None


class HostedProjectRepository:
    """SQL-backed repository that scopes every query to the authenticated user."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _record(project: Project) -> ProjectRecord:
        states = project.stage_states if isinstance(project.stage_states, dict) else {}
        completed = tuple(
            stage_id for stage_id in PRIMARY_WORKFLOW_STAGES
            if (value := states.get(stage_id)) is not None
            if str(value.get("status") if isinstance(value, dict) else value).casefold()
            in COMPLETED_STAGE_STATUSES
        )
        discovery = states.get("discovery")
        discovery_status = str(
            discovery.get("status") if isinstance(discovery, dict) else discovery or "pending"
        )
        return ProjectRecord(
            project_id=str(project.id),
            slug=project.slug,
            owner_user_id=str(project.user_id),
            topic=project.topic,
            discovery_status=discovery_status,
            completed_stages=completed,
            taxonomy_profile=project.taxonomy_profile,
            model_tier=project.model_tier or DEFAULT_MODEL_TIER,
            current_stage=(
                current_stage_from_states(states)
                if states
                else project.current_stage or PRIMARY_WORKFLOW_STAGES[0]
            ),
            stage_states=dict(states),
        )

    def list_for_user(self, user_id: str) -> list[ProjectRecord]:
        user_uuid = uuid.UUID(user_id)
        with database_session(self.session_factory) as session:
            projects = session.scalars(
                select(Project)
                .where(Project.user_id == user_uuid, Project.deleted_at.is_(None))
                .order_by(Project.updated_at.desc())
            ).all()
            return [self._record(project) for project in projects]

    def get_for_user(self, user_id: str, project_id: str) -> ProjectRecord | None:
        user_uuid = uuid.UUID(user_id)
        try:
            parsed_id = uuid.UUID(project_id)
        except ValueError:
            parsed_id = None
        with database_session(self.session_factory) as session:
            query = select(Project).where(Project.user_id == user_uuid, Project.deleted_at.is_(None))
            query = query.where(Project.id == parsed_id) if parsed_id else query.where(Project.slug == project_id)
            project = session.scalar(query)
            return self._record(project) if project else None

    def create_for_user(
        self, user_id: str, *, slug: str, topic: str, taxonomy_profile: str,
        model_tier: str = DEFAULT_MODEL_TIER,
    ) -> ProjectRecord:
        try:
            safe_slug = validate_project_id(slug)
        except WorkspaceConfigurationError as exc:
            raise ProjectOperationError(str(exc)) from exc
        user_uuid = uuid.UUID(user_id)
        try:
            selected_tier = resolve_model_tier(model_tier).id
        except ValueError as exc:
            raise ProjectOperationError(str(exc)) from exc
        with database_session(self.session_factory) as session:
            project = Project(
                user_id=user_uuid,
                slug=safe_slug,
                topic=str(topic or "").strip(),
                taxonomy_profile=validate_taxonomy_profile(
                    taxonomy_profile or DEFAULT_TAXONOMY_PROFILE
                ),
                model_tier=selected_tier,
                status="active",
                current_stage="discovery",
                stage_states={},
            )
            session.add(project)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ProjectOperationError("A project with this ID already exists.") from exc
            return self._record(project)

    def update_model_tier_for_user(
        self, user_id: str, project_id: str, *, model_tier: str
    ) -> ProjectRecord:
        try:
            selected_tier = resolve_model_tier(model_tier).id
        except ValueError as exc:
            raise ProjectOperationError(str(exc)) from exc
        with database_session(self.session_factory) as session:
            project = self._owned_project(session, user_id, project_id)
            if project is None:
                raise ProjectOperationError("Project not found.")
            project.model_tier = selected_tier
            project.updated_at = utc_now()
            session.flush()
            return self._record(project)

    def update_taxonomy_profile_for_user(
        self,
        user_id: str,
        project_id: str,
        *,
        taxonomy_profile: str,
        confirm_downstream_invalidation: bool = False,
    ) -> ProjectTaxonomyUpdateResult:
        selected_profile = validate_taxonomy_profile(taxonomy_profile)
        with database_session(self.session_factory) as session:
            project = self._owned_project(session, user_id, project_id, for_update=True)
            if project is None:
                raise ProjectOperationError("Project not found.")
            changed = project.taxonomy_profile != selected_profile
            stored_states = dict(project.stage_states or {})
            states = session.scalars(
                select(WorkflowStageState)
                .where(WorkflowStageState.project_id == project.id)
                .with_for_update()
            ).all()
            states_by_stage = {state.stage_id: state for state in states}
            matrix_entered = "matrix" in stored_states or "matrix" in states_by_stage
            if changed and matrix_entered and not confirm_downstream_invalidation:
                raise ProjectOperationError(
                    "Changing the taxonomy profile after Matrix entry requires "
                    "confirm_downstream_invalidation=true."
                )
            if not changed:
                return ProjectTaxonomyUpdateResult(
                    project=self._record(project),
                    changed=False,
                    matrix_entered=matrix_entered,
                    downstream_stale=False,
                )

            now = utc_now()
            stale_stages = {"discovery"}
            if matrix_entered:
                stale_stages.update(PRIMARY_WORKFLOW_STAGES[1:])
            for stage_id in stale_stages:
                state = states_by_stage.get(stage_id)
                stored = stored_states.get(stage_id)
                if state is not None:
                    state.status = "stale"
                    state.error_code = ""
                    state.error_message = ""
                    state.revision += 1
                    state.updated_at = now
                    stored_states[stage_id] = {
                        "status": "stale",
                        "revision": state.revision,
                    }
                elif stored is not None:
                    revision = int(stored.get("revision") or 0) if isinstance(stored, dict) else 0
                    stored_states[stage_id] = {
                        "status": "stale",
                        "revision": revision + 1,
                    }

            project.taxonomy_profile = selected_profile
            project.stage_states = stored_states
            project.current_stage = "discovery"
            project.updated_at = now
            session.flush()
            return ProjectTaxonomyUpdateResult(
                project=self._record(project),
                changed=True,
                matrix_entered=matrix_entered,
                downstream_stale=matrix_entered,
            )

    def _owned_project(
        self, session, user_id: str, project_id: str, *, for_update: bool = False
    ) -> Project | None:
        user_uuid = uuid.UUID(user_id)
        try:
            parsed_id = uuid.UUID(project_id)
        except ValueError:
            parsed_id = None
        query = select(Project).where(Project.user_id == user_uuid, Project.deleted_at.is_(None))
        query = query.where(Project.id == parsed_id) if parsed_id else query.where(Project.slug == project_id)
        return session.scalar(query.with_for_update() if for_update else query)

    def delete_for_user(self, user_id: str, project_id: str) -> bool:
        with database_session(self.session_factory) as session:
            project = self._owned_project(session, user_id, project_id)
            if project is None:
                return False
            project.status = "deleted"
            project.deleted_at = utc_now()
            return True

    def restore_for_user(self, user_id: str, project_id: str) -> bool:
        user_uuid = uuid.UUID(user_id)
        try:
            parsed_id = uuid.UUID(project_id)
        except ValueError:
            parsed_id = None
        with database_session(self.session_factory) as session:
            query = select(Project).where(Project.user_id == user_uuid)
            query = (
                query.where(Project.id == parsed_id)
                if parsed_id
                else query.where(Project.slug == project_id)
            )
            project = session.scalar(query)
            if project is None:
                return False
            project.status = "active"
            project.deleted_at = None
            project.updated_at = utc_now()
            return True

    def update_topic_for_user(
        self, user_id: str, project_id: str, *, topic: str, taxonomy_profile: str
    ) -> ProjectRecord:
        with database_session(self.session_factory) as session:
            project = self._owned_project(session, user_id, project_id)
            if project is None:
                raise ProjectOperationError("Project not found.")
            project.topic = str(topic or "").strip()
            project.taxonomy_profile = validate_taxonomy_profile(
                taxonomy_profile or project.taxonomy_profile or DEFAULT_TAXONOMY_PROFILE
            )
            project.current_stage = "discovery"
            project.stage_states = {}
            project.updated_at = utc_now()
            session.flush()
            return self._record(project)

    def sync_stage_states_for_user(
        self,
        user_id: str,
        project_id: str,
        stage_states: dict[str, object],
        current_stage: str,
    ) -> None:
        with database_session(self.session_factory) as session:
            project = self._owned_project(session, user_id, project_id)
            if project is None:
                raise ProjectOperationError("Project not found.")
            project.stage_states = dict(stage_states)
            project.current_stage = current_stage or current_stage_from_states(stage_states)
            project.updated_at = utc_now()
