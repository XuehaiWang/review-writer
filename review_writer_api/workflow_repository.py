"""User-scoped persistence for PostgreSQL-native workflow state and jobs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from review_writer_api.database import Project, database_session, utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowMigrationRequired,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.workflow_contracts import INTERNAL_STAGES, current_user_stage
from review_writer_api.workflow_models import (
    WorkflowApproval,
    WorkflowArtifact,
    WorkflowCurrentArtifact,
    WorkflowCurrentJob,
    WorkflowJob,
    WorkflowMigration,
    WorkflowStageRun,
    WorkflowStageState,
    WorkflowSystemState,
)


@dataclass(frozen=True)
class StageStateRecord:
    id: str
    project_id: str
    stage_id: str
    status: str
    revision: int
    current_run_id: str | None
    input_fingerprint: str
    output_fingerprint: str
    error_code: str
    error_message: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JobRecord:
    id: str
    user_id: str
    project_id: str | None
    scope: str
    job_type: str
    status: str
    idempotency_scope_key: str
    idempotency_key: str
    payload: dict[str, Any]
    result: dict[str, Any]
    progress_current: int
    progress_total: int
    cancellation_requested: bool
    error_code: str
    error_message: str
    retry_of_job_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class StageRunRecord:
    id: str
    project_id: str
    stage_id: str
    requested_by_user_id: str | None
    status: str
    attempt: int
    input_snapshot: Any
    output_snapshot: Any
    progress_current: int
    progress_total: int
    metadata: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    project_id: str
    logical_name: str
    artifact_type: str
    relative_path: str
    content_sha256: str
    lineage_sha256: str
    size_bytes: int
    mtime_ns: int
    availability: str
    producer_stage: str
    producer_run_id: str | None
    metadata: dict[str, Any]
    created_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    project_id: str
    stage_id: str
    subject_type: str
    subject_id: str
    decision: str
    decided_by_user_id: str | None
    details: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class MigrationRecord:
    id: str
    source_kind: str
    source_identity: str
    source_sha256: str
    status: str
    report: dict[str, Any]
    error_message: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class OwnedProjectRecord:
    id: str
    user_id: str
    slug: str
    topic: str
    taxonomy_profile: str


@dataclass(frozen=True)
class OwnedArtifactRecord:
    artifact: ArtifactRecord
    project_slug: str


class WorkflowRepository:
    """Transactional repository that includes ownership in every user-facing query."""

    STAGE_CHANGE_FIELDS = frozenset(
        {
            "status",
            "current_run_id",
            "input_fingerprint",
            "output_fingerprint",
            "error_code",
            "error_message",
        }
    )

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _uuid(value: str, *, not_found_message: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise WorkflowNotFound(not_found_message) from exc

    @staticmethod
    def _stage_record(state: WorkflowStageState) -> StageStateRecord:
        return StageStateRecord(
            id=str(state.id),
            project_id=str(state.project_id),
            stage_id=state.stage_id,
            status=state.status,
            revision=state.revision,
            current_run_id=str(state.current_run_id) if state.current_run_id else None,
            input_fingerprint=state.input_fingerprint,
            output_fingerprint=state.output_fingerprint,
            error_code=state.error_code,
            error_message=state.error_message,
            created_at=state.created_at,
            updated_at=state.updated_at,
        )

    @staticmethod
    def _job_record(job: WorkflowJob) -> JobRecord:
        return JobRecord(
            id=str(job.id),
            user_id=str(job.user_id),
            project_id=str(job.project_id) if job.project_id else None,
            scope=job.scope,
            job_type=job.job_type,
            status=job.status,
            idempotency_scope_key=job.idempotency_scope_key,
            idempotency_key=job.idempotency_key,
            payload=dict(job.payload_json or {}),
            result=dict(job.result_json or {}),
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            cancellation_requested=job.cancellation_requested,
            error_code=job.error_code,
            error_message=job.error_message,
            retry_of_job_id=(str(job.retry_of_job_id) if job.retry_of_job_id else None),
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    @staticmethod
    def _stage_run_record(run: WorkflowStageRun) -> StageRunRecord:
        return StageRunRecord(
            id=str(run.id),
            project_id=str(run.project_id),
            stage_id=run.stage_id,
            requested_by_user_id=(
                str(run.requested_by_user_id) if run.requested_by_user_id else None
            ),
            status=run.status,
            attempt=run.attempt,
            input_snapshot=run.input_snapshot,
            output_snapshot=run.output_snapshot,
            progress_current=run.progress_current,
            progress_total=run.progress_total,
            metadata=dict(run.metadata_json or {}),
            started_at=run.started_at,
            updated_at=run.updated_at,
            finished_at=run.finished_at,
        )

    @staticmethod
    def _artifact_record(artifact: WorkflowArtifact) -> ArtifactRecord:
        return ArtifactRecord(
            id=str(artifact.id),
            project_id=str(artifact.project_id),
            logical_name=artifact.logical_name,
            artifact_type=artifact.artifact_type,
            relative_path=artifact.relative_path,
            content_sha256=artifact.content_sha256,
            lineage_sha256=artifact.lineage_sha256,
            size_bytes=artifact.size_bytes,
            mtime_ns=artifact.mtime_ns,
            availability=artifact.availability,
            producer_stage=artifact.producer_stage,
            producer_run_id=str(artifact.producer_run_id) if artifact.producer_run_id else None,
            metadata=dict(artifact.metadata_json or {}),
            created_at=artifact.created_at,
        )

    @staticmethod
    def _approval_record(approval: WorkflowApproval) -> ApprovalRecord:
        return ApprovalRecord(
            id=str(approval.id),
            project_id=str(approval.project_id),
            stage_id=approval.stage_id,
            subject_type=approval.subject_type,
            subject_id=approval.subject_id,
            decision=approval.decision,
            decided_by_user_id=(
                str(approval.decided_by_user_id) if approval.decided_by_user_id else None
            ),
            details=dict(approval.details_json or {}),
            created_at=approval.created_at,
        )

    @staticmethod
    def _migration_record(migration: WorkflowMigration) -> MigrationRecord:
        return MigrationRecord(
            id=str(migration.id),
            source_kind=migration.source_kind,
            source_identity=migration.source_identity,
            source_sha256=migration.source_sha256,
            status=migration.status,
            report=dict(migration.report_json or {}),
            error_message=migration.error_message,
            started_at=migration.started_at,
            finished_at=migration.finished_at,
        )

    @staticmethod
    def _owned_project(session, user_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
        return session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )

    def get_owned_project(self, user_id: str, project_id: str) -> OwnedProjectRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        with database_session(self.session_factory) as session:
            project = self._owned_project(session, user_uuid, project_uuid)
            if project is None:
                return None
            return OwnedProjectRecord(
                id=str(project.id),
                user_id=str(project.user_id),
                slug=project.slug,
                topic=project.topic,
                taxonomy_profile=project.taxonomy_profile,
            )

    def get_stage_state(
        self, user_id: str, project_id: str, stage_id: str
    ) -> StageStateRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        with database_session(self.session_factory) as session:
            state = session.scalar(
                select(WorkflowStageState)
                .join(Project, Project.id == WorkflowStageState.project_id)
                .where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id == stage_id,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
            )
            return self._stage_record(state) if state else None

    def invalidate_downstream_after_discovery(self, user_id: str, project_id: str) -> None:
        """Clear derived outputs when a newly published Discovery review replaces its input."""
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        downstream = tuple(stage for stage in INTERNAL_STAGES if stage != "discovery")
        with database_session(self.session_factory) as session:
            project = session.scalar(select(Project).where(Project.id == project_uuid, Project.user_id == user_uuid, Project.deleted_at.is_(None)).with_for_update())
            if project is None:
                raise WorkflowNotFound("Project not found.")
            downstream_artifacts = select(WorkflowArtifact.id).where(
                WorkflowArtifact.project_id == project_uuid,
                WorkflowArtifact.producer_stage.in_(downstream),
            )
            session.execute(
                delete(WorkflowCurrentArtifact).where(
                    WorkflowCurrentArtifact.project_id == project_uuid,
                    WorkflowCurrentArtifact.artifact_id.in_(downstream_artifacts),
                )
            )
            states = session.scalars(
                select(WorkflowStageState).where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id.in_(downstream),
                )
            ).all()
            now = utc_now()
            stored_states = dict(project.stage_states or {})
            for state in states:
                state.status = "pending"
                state.current_run_id = None
                state.error_code = ""
                state.error_message = ""
                state.revision += 1
                state.updated_at = now
                stored_states[state.stage_id] = {"status": "pending", "revision": state.revision}
            project.stage_states = stored_states
            project.current_stage = current_user_stage(
                {
                    stage_id: value.get("status", "pending")
                    if isinstance(value, dict)
                    else str(value)
                    for stage_id, value in stored_states.items()
                }
            )
            project.updated_at = now

    def replace_discovery_atomically(
        self,
        user_id: str,
        project_id: str,
        *,
        artifact_id: str,
        run_id: str,
        expected_revision: int,
        topic: str,
    ) -> StageStateRecord:
        """Promote a staged Discovery artifact and invalidate derived state in one commit."""
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        artifact_uuid = self._uuid(artifact_id, not_found_message="Artifact not found.")
        run_uuid = self._uuid(run_id, not_found_message="Stage run not found.")
        downstream = tuple(stage for stage in INTERNAL_STAGES if stage != "discovery")
        with database_session(self.session_factory) as session:
            project = session.scalar(
                select(Project)
                .where(
                    Project.id == project_uuid,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if project is None:
                raise WorkflowNotFound("Project not found.")
            artifact = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.id == artifact_uuid,
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.logical_name == "discovery/review.json",
                    WorkflowArtifact.producer_stage == "discovery",
                )
            )
            if artifact is None:
                raise WorkflowNotFound("Artifact not found.")
            run = session.scalar(
                select(WorkflowStageRun).where(
                    WorkflowStageRun.id == run_uuid,
                    WorkflowStageRun.project_id == project_uuid,
                    WorkflowStageRun.stage_id == "discovery",
                )
            )
            if run is None:
                raise WorkflowNotFound("Stage run not found.")
            state = session.scalar(
                select(WorkflowStageState)
                .where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id == "discovery",
                )
                .with_for_update()
            )
            actual = state.revision if state else 0
            if actual != expected_revision:
                raise WorkflowConflict(
                    "Discovery changed since it was loaded.",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": actual,
                    },
                )
            now = utc_now()
            self._upsert_current_artifact(
                session,
                project_id=project_uuid,
                logical_name="discovery/review.json",
                artifact_id=artifact_uuid,
            )
            if state is None:
                state = WorkflowStageState(
                    project_id=project_uuid,
                    stage_id="discovery",
                    status="review",
                    revision=1,
                    current_run_id=run_uuid,
                )
                session.add(state)
            else:
                state.status = "review"
                state.current_run_id = run_uuid
                state.revision = actual + 1
                state.error_code = ""
                state.error_message = ""
                state.updated_at = now
            downstream_artifacts = select(WorkflowArtifact.id).where(
                WorkflowArtifact.project_id == project_uuid,
                WorkflowArtifact.producer_stage.in_(downstream),
            )
            session.execute(
                delete(WorkflowCurrentArtifact).where(
                    WorkflowCurrentArtifact.project_id == project_uuid,
                    WorkflowCurrentArtifact.artifact_id.in_(downstream_artifacts),
                )
            )
            stored = dict(project.stage_states or {})
            stored["discovery"] = {"status": state.status, "revision": state.revision}
            derived_states = session.scalars(
                select(WorkflowStageState).where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id.in_(downstream),
                )
            ).all()
            for derived in derived_states:
                derived.status = "pending"
                derived.current_run_id = None
                derived.error_code = ""
                derived.error_message = ""
                derived.revision += 1
                derived.updated_at = now
                stored[derived.stage_id] = {
                    "status": "pending",
                    "revision": derived.revision,
                }
            project.topic = str(topic)
            project.stage_states = stored
            project.current_stage = current_user_stage(
                {
                    key: (
                        value.get("status", "pending")
                        if isinstance(value, dict)
                        else str(value)
                    )
                    for key, value in stored.items()
                }
            )
            project.updated_at = now
            session.flush()
            return self._stage_record(state)

    def save_discovery_atomically(
        self,
        user_id: str,
        project_id: str,
        *,
        artifact_id: str,
        run_id: str,
        expected_revision: int,
        status: str,
    ) -> StageStateRecord:
        """Promote one reviewed Discovery version with its stage revision in one commit."""

        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        artifact_uuid = self._uuid(
            artifact_id, not_found_message="Artifact not found."
        )
        run_uuid = self._uuid(run_id, not_found_message="Stage run not found.")
        with database_session(self.session_factory) as session:
            project = session.scalar(
                select(Project)
                .where(
                    Project.id == project_uuid,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if project is None:
                raise WorkflowNotFound("Project not found.")
            artifact = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.id == artifact_uuid,
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.logical_name == "discovery/review.json",
                    WorkflowArtifact.producer_stage == "discovery",
                )
            )
            run = session.scalar(
                select(WorkflowStageRun).where(
                    WorkflowStageRun.id == run_uuid,
                    WorkflowStageRun.project_id == project_uuid,
                    WorkflowStageRun.stage_id == "discovery",
                )
            )
            if artifact is None:
                raise WorkflowNotFound("Artifact not found.")
            if run is None:
                raise WorkflowNotFound("Stage run not found.")
            state = session.scalar(
                select(WorkflowStageState)
                .where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id == "discovery",
                )
                .with_for_update()
            )
            actual = state.revision if state else 0
            if actual != expected_revision:
                raise WorkflowConflict(
                    "Discovery changed since it was loaded.",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": actual,
                    },
                )
            now = utc_now()
            self._upsert_current_artifact(
                session,
                project_id=project_uuid,
                logical_name="discovery/review.json",
                artifact_id=artifact_uuid,
            )
            if state is None:
                state = WorkflowStageState(
                    project_id=project_uuid,
                    stage_id="discovery",
                    status=status,
                    revision=1,
                    current_run_id=run_uuid,
                )
                session.add(state)
            else:
                state.status = status
                state.revision = actual + 1
                state.current_run_id = run_uuid
                state.error_code = ""
                state.error_message = ""
                state.updated_at = now
            stored = dict(project.stage_states or {})
            stored["discovery"] = {
                "status": state.status,
                "revision": state.revision,
            }
            project.stage_states = stored
            project.current_stage = current_user_stage(
                {
                    key: (
                        value.get("status", "pending")
                        if isinstance(value, dict)
                        else str(value)
                    )
                    for key, value in stored.items()
                }
            )
            project.updated_at = now
            session.flush()
            return self._stage_record(state)

    def confirm_discovery_atomically(
        self,
        user_id: str,
        project_id: str,
        *,
        artifact_id: str,
        run_id: str,
        expected_discovery_revision: int,
        expected_matrix_revision: int,
    ) -> tuple[StageStateRecord, StageStateRecord]:
        """Promote a staged Matrix artifact and approve its exact Discovery revision atomically."""
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        artifact_uuid = self._uuid(
            artifact_id, not_found_message="Artifact not found."
        )
        run_uuid = self._uuid(run_id, not_found_message="Stage run not found.")
        with database_session(self.session_factory) as session:
            project = session.scalar(
                select(Project)
                .where(
                    Project.id == project_uuid,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if project is None:
                raise WorkflowNotFound("Project not found.")
            artifact = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.id == artifact_uuid,
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.logical_name == "matrix/literature_matrix.json",
                    WorkflowArtifact.producer_stage == "matrix",
                )
            )
            if artifact is None:
                raise WorkflowNotFound("Artifact not found.")
            run = session.scalar(
                select(WorkflowStageRun).where(
                    WorkflowStageRun.id == run_uuid,
                    WorkflowStageRun.project_id == project_uuid,
                    WorkflowStageRun.stage_id == "matrix",
                )
            )
            if run is None:
                raise WorkflowNotFound("Stage run not found.")
            discovery = session.scalar(
                select(WorkflowStageState)
                .where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id == "discovery",
                )
                .with_for_update()
            )
            matrix = session.scalar(
                select(WorkflowStageState)
                .where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id == "matrix",
                )
                .with_for_update()
            )
            actual_discovery = discovery.revision if discovery else 0
            actual_matrix = matrix.revision if matrix else 0
            if (
                discovery is None
                or actual_discovery != expected_discovery_revision
                or actual_matrix != expected_matrix_revision
            ):
                raise WorkflowConflict("Discovery changed since confirmation was opened.")
            now = utc_now()
            self._upsert_current_artifact(
                session,
                project_id=project_uuid,
                logical_name="matrix/literature_matrix.json",
                artifact_id=artifact_uuid,
            )
            downstream = tuple(
                stage
                for stage in INTERNAL_STAGES
                if INTERNAL_STAGES.index(stage) >= INTERNAL_STAGES.index("matrix")
            )
            derived_artifacts = select(WorkflowArtifact.id).where(
                WorkflowArtifact.project_id == project_uuid,
                WorkflowArtifact.producer_stage.in_(downstream),
            )
            session.execute(
                delete(WorkflowCurrentArtifact).where(
                    WorkflowCurrentArtifact.project_id == project_uuid,
                    WorkflowCurrentArtifact.logical_name
                    != "matrix/literature_matrix.json",
                    WorkflowCurrentArtifact.artifact_id.in_(derived_artifacts),
                )
            )
            if matrix is None:
                matrix = WorkflowStageState(
                    project_id=project_uuid,
                    stage_id="matrix",
                    status="review",
                    revision=1,
                    current_run_id=run_uuid,
                )
                session.add(matrix)
            else:
                matrix.status = "review"
                matrix.current_run_id = run_uuid
                matrix.revision = expected_matrix_revision + 1
                matrix.error_code = ""
                matrix.error_message = ""
                matrix.updated_at = now
            discovery.status = "approved"
            discovery.revision = expected_discovery_revision + 1
            discovery.error_code = ""
            discovery.error_message = ""
            discovery.updated_at = now
            stored = dict(project.stage_states or {})
            stored["matrix"] = {
                "status": matrix.status,
                "revision": matrix.revision,
            }
            stored["discovery"] = {
                "status": discovery.status,
                "revision": discovery.revision,
            }
            stale_states = session.scalars(
                select(WorkflowStageState).where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id.in_(
                        tuple(
                            stage
                            for stage in downstream
                            if stage not in {"matrix"}
                        )
                    ),
                )
            ).all()
            for stale in stale_states:
                stale.status = "pending"
                stale.current_run_id = None
                stale.error_code = ""
                stale.error_message = ""
                stale.revision += 1
                stale.updated_at = now
                stored[stale.stage_id] = {
                    "status": "pending",
                    "revision": stale.revision,
                }
            project.stage_states = stored
            project.current_stage = current_user_stage(
                {
                    key: (
                        value.get("status", "pending")
                        if isinstance(value, dict)
                        else str(value)
                    )
                    for key, value in stored.items()
                }
            )
            project.updated_at = now
            session.flush()
            return self._stage_record(discovery), self._stage_record(matrix)

    def create_stage_run(
        self,
        user_id: str,
        project_id: str,
        stage_id: str,
        *,
        status: str = "pending",
        attempt: int = 1,
        input_snapshot: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> StageRunRecord:
        if stage_id not in INTERNAL_STAGES:
            raise WorkflowValidationError(f"Unknown workflow stage: {stage_id}")
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        with database_session(self.session_factory) as session:
            if self._owned_project(session, user_uuid, project_uuid) is None:
                raise WorkflowNotFound("Project not found.")
            run = WorkflowStageRun(
                project_id=project_uuid,
                stage_id=stage_id,
                requested_by_user_id=user_uuid,
                status=status,
                attempt=max(1, int(attempt)),
                input_snapshot=input_snapshot if input_snapshot is not None else {},
                metadata_json=dict(metadata or {}),
            )
            session.add(run)
            session.flush()
            return self._stage_run_record(run)

    def get_stage_run(
        self, user_id: str, project_id: str, run_id: str
    ) -> StageRunRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Stage run not found.")
        project_uuid = self._uuid(project_id, not_found_message="Stage run not found.")
        run_uuid = self._uuid(run_id, not_found_message="Stage run not found.")
        with database_session(self.session_factory) as session:
            run = session.scalar(
                select(WorkflowStageRun)
                .join(Project, Project.id == WorkflowStageRun.project_id)
                .where(
                    WorkflowStageRun.id == run_uuid,
                    WorkflowStageRun.project_id == project_uuid,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
            )
            return self._stage_run_record(run) if run else None

    def compare_and_set_stage(
        self,
        user_id: str,
        project_id: str,
        stage_id: str,
        expected_revision: int,
        **changes: Any,
    ) -> StageStateRecord:
        if stage_id not in INTERNAL_STAGES:
            raise WorkflowValidationError(f"Unknown workflow stage: {stage_id}")
        unsupported = set(changes) - self.STAGE_CHANGE_FIELDS
        if unsupported:
            raise WorkflowValidationError(
                "Unsupported stage-state fields.", details={"fields": sorted(unsupported)}
            )
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        values = dict(changes)
        if "current_run_id" in values and values["current_run_id"] is not None:
            values["current_run_id"] = self._uuid(
                values["current_run_id"], not_found_message="Stage run not found."
            )
        now = utc_now()

        try:
            with database_session(self.session_factory) as session:
                project = self._owned_project(session, user_uuid, project_uuid)
                if project is None:
                    raise WorkflowNotFound("Project not found.")
                state = session.scalar(
                    select(WorkflowStageState).where(
                        WorkflowStageState.project_id == project_uuid,
                        WorkflowStageState.stage_id == stage_id,
                    )
                )
                actual_revision = state.revision if state else 0
                if actual_revision != expected_revision:
                    raise WorkflowConflict(
                        "Workflow stage changed since it was loaded.",
                        details={
                            "expected_revision": expected_revision,
                            "actual_revision": actual_revision,
                        },
                    )

                if state is None:
                    state = WorkflowStageState(
                        project_id=project_uuid,
                        stage_id=stage_id,
                        revision=1,
                        **values,
                    )
                    session.add(state)
                    session.flush()
                else:
                    update_values = {**values, "revision": expected_revision + 1, "updated_at": now}
                    state = session.scalar(
                        update(WorkflowStageState)
                        .where(
                            WorkflowStageState.id == state.id,
                            WorkflowStageState.revision == expected_revision,
                        )
                        .values(**update_values)
                        .returning(WorkflowStageState)
                    )
                    if state is None:
                        raise WorkflowConflict(
                            "Workflow stage changed since it was loaded.",
                            details={"expected_revision": expected_revision},
                        )

                stored_states = dict(project.stage_states or {})
                stored_states[stage_id] = {"status": state.status, "revision": state.revision}
                project.stage_states = stored_states
                project.current_stage = current_user_stage(
                    {
                        item_stage: (
                            item_value.get("status", "pending")
                            if isinstance(item_value, dict)
                            else str(item_value)
                        )
                        for item_stage, item_value in stored_states.items()
                    }
                )
                project.updated_at = now
                session.flush()
                return self._stage_record(state)
        except IntegrityError as exc:
            raise WorkflowConflict(
                "Workflow stage changed since it was loaded.",
                details={"expected_revision": expected_revision},
            ) from exc

    def promote_stage_artifacts_atomically(
        self,
        user_id: str,
        project_id: str,
        stage_id: str,
        *,
        artifact_ids: dict[str, str],
        run_id: str,
        expected_revision: int,
        status: str = "review",
        invalidate_stages: tuple[str, ...] = (),
        approve_stages: dict[str, int] | None = None,
        approval_events: list[dict[str, Any]] | None = None,
        expected_current_artifacts: dict[str, str] | None = None,
        expected_stage_states: dict[str, dict[str, Any]] | None = None,
    ) -> StageStateRecord:
        """Promote one or more immutable outputs and advance their stage in one commit."""

        if stage_id not in INTERNAL_STAGES:
            raise WorkflowValidationError(f"Unknown workflow stage: {stage_id}")
        if not artifact_ids:
            raise WorkflowValidationError("At least one staged artifact is required.")
        invalid = sorted(set(invalidate_stages) - set(INTERNAL_STAGES))
        if invalid:
            raise WorkflowValidationError(
                "Unknown downstream workflow stage.", details={"stages": invalid}
            )
        approvals = dict(approve_stages or {})
        pending_approvals = [dict(event) for event in approval_events or []]
        expected_currents = {
            str(logical_name): self._uuid(
                artifact_id, not_found_message="Expected current artifact not found."
            )
            for logical_name, artifact_id in (
                expected_current_artifacts or {}
            ).items()
        }
        expected_states = {
            str(expected_stage): {
                "revision": int((expected_value or {}).get("revision", 0)),
                "status": str((expected_value or {}).get("status") or ""),
            }
            for expected_stage, expected_value in (
                expected_stage_states or {}
            ).items()
        }
        invalid_expected_states = sorted(set(expected_states) - set(INTERNAL_STAGES))
        if invalid_expected_states:
            raise WorkflowValidationError(
                "Unknown expected workflow stage.",
                details={"stages": invalid_expected_states},
            )
        invalid_approvals = sorted(set(approvals) - set(INTERNAL_STAGES))
        if invalid_approvals or stage_id in approvals:
            raise WorkflowValidationError(
                "Invalid predecessor stage approval.",
                details={"stages": invalid_approvals or [stage_id]},
            )
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        run_uuid = self._uuid(run_id, not_found_message="Stage run not found.")
        parsed_artifacts = {
            str(logical_name): self._uuid(
                artifact_id, not_found_message="Artifact not found."
            )
            for logical_name, artifact_id in artifact_ids.items()
        }
        with database_session(self.session_factory) as session:
            project = session.scalar(
                select(Project)
                .where(
                    Project.id == project_uuid,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if project is None:
                raise WorkflowNotFound("Project not found.")
            if expected_currents:
                current_rows = session.scalars(
                    select(WorkflowCurrentArtifact)
                    .where(
                        WorkflowCurrentArtifact.project_id == project_uuid,
                        WorkflowCurrentArtifact.logical_name.in_(
                            tuple(expected_currents)
                        ),
                    )
                    .with_for_update()
                ).all()
                actual_currents = {
                    row.logical_name: row.artifact_id for row in current_rows
                }
                for logical_name, expected_id in expected_currents.items():
                    if actual_currents.get(logical_name) != expected_id:
                        raise WorkflowConflict(
                            "A current workflow artifact changed since it was loaded.",
                            details={"logical_name": logical_name},
                        )
            run = session.scalar(
                select(WorkflowStageRun).where(
                    WorkflowStageRun.id == run_uuid,
                    WorkflowStageRun.project_id == project_uuid,
                    WorkflowStageRun.stage_id == stage_id,
                )
            )
            if run is None:
                raise WorkflowNotFound("Stage run not found.")
            artifacts = session.scalars(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.id.in_(tuple(parsed_artifacts.values())),
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.producer_stage == stage_id,
                )
            ).all()
            by_id = {artifact.id: artifact for artifact in artifacts}
            if len(by_id) != len(set(parsed_artifacts.values())):
                raise WorkflowNotFound("Artifact not found.")
            for logical_name, artifact_uuid in parsed_artifacts.items():
                if by_id[artifact_uuid].logical_name != logical_name:
                    raise WorkflowValidationError(
                        "Artifact logical name does not match its promotion target."
                    )
            state = session.scalar(
                select(WorkflowStageState)
                .where(
                    WorkflowStageState.project_id == project_uuid,
                    WorkflowStageState.stage_id == stage_id,
                )
                .with_for_update()
            )
            actual = state.revision if state else 0
            if actual != int(expected_revision):
                raise WorkflowConflict(
                    "Workflow stage changed since it was loaded.",
                    details={
                        "expected_revision": int(expected_revision),
                        "actual_revision": actual,
                    },
                )
            if expected_states:
                expected_rows = session.scalars(
                    select(WorkflowStageState)
                    .where(
                        WorkflowStageState.project_id == project_uuid,
                        WorkflowStageState.stage_id.in_(tuple(expected_states)),
                    )
                    .with_for_update()
                ).all()
                actual_states = {row.stage_id: row for row in expected_rows}
                for expected_stage, expected_value in expected_states.items():
                    row = actual_states.get(expected_stage)
                    actual_revision = row.revision if row else 0
                    actual_status = row.status if row else "pending"
                    if (
                        actual_revision != expected_value["revision"]
                        or actual_status != expected_value["status"]
                    ):
                        raise WorkflowConflict(
                            "A required workflow stage changed since it was loaded.",
                            details={
                                "stage_id": expected_stage,
                                "expected_revision": expected_value["revision"],
                                "actual_revision": actual_revision,
                                "expected_status": expected_value["status"],
                                "actual_status": actual_status,
                            },
                        )
            predecessor_states: dict[str, WorkflowStageState] = {}
            if approvals:
                rows = session.scalars(
                    select(WorkflowStageState)
                    .where(
                        WorkflowStageState.project_id == project_uuid,
                        WorkflowStageState.stage_id.in_(tuple(approvals)),
                    )
                    .with_for_update()
                ).all()
                predecessor_states = {row.stage_id: row for row in rows}
                for predecessor, expected in approvals.items():
                    row = predecessor_states.get(predecessor)
                    actual_predecessor = row.revision if row else 0
                    if row is None or actual_predecessor != int(expected):
                        raise WorkflowConflict(
                            "A predecessor stage changed since it was loaded.",
                            details={
                                "stage_id": predecessor,
                                "expected_revision": int(expected),
                                "actual_revision": actual_predecessor,
                            },
                        )
            now = utc_now()
            for event in pending_approvals:
                approval_id = self._uuid(
                    event.get("id"), not_found_message="Approval ID is invalid."
                )
                event_stage = str(event.get("stage_id") or "")
                if event_stage != stage_id:
                    raise WorkflowValidationError(
                        "Approval event does not belong to the promoted stage."
                    )
                session.add(
                    WorkflowApproval(
                        id=approval_id,
                        project_id=project_uuid,
                        stage_id=event_stage,
                        subject_type=str(event.get("subject_type") or ""),
                        subject_id=str(event.get("subject_id") or ""),
                        decision=str(event.get("decision") or ""),
                        decided_by_user_id=user_uuid,
                        details_json=dict(event.get("details") or {}),
                        created_at=event.get("created_at") or now,
                    )
                )
            for logical_name, artifact_uuid in parsed_artifacts.items():
                self._upsert_current_artifact(
                    session,
                    project_id=project_uuid,
                    logical_name=logical_name,
                    artifact_id=artifact_uuid,
                )
            if state is None:
                state = WorkflowStageState(
                    project_id=project_uuid,
                    stage_id=stage_id,
                    status=str(status),
                    revision=1,
                    current_run_id=run_uuid,
                )
                session.add(state)
            else:
                state.status = str(status)
                state.revision = actual + 1
                state.current_run_id = run_uuid
                state.error_code = ""
                state.error_message = ""
                state.updated_at = now

            stored = dict(project.stage_states or {})
            stored[stage_id] = {"status": state.status, "revision": state.revision}
            for predecessor, row in predecessor_states.items():
                row.status = "approved"
                row.revision += 1
                row.error_code = ""
                row.error_message = ""
                row.updated_at = now
                stored[predecessor] = {
                    "status": "approved",
                    "revision": row.revision,
                }
            if invalidate_stages:
                derived_artifacts = select(WorkflowArtifact.id).where(
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.producer_stage.in_(invalidate_stages),
                )
                session.execute(
                    delete(WorkflowCurrentArtifact).where(
                        WorkflowCurrentArtifact.project_id == project_uuid,
                        WorkflowCurrentArtifact.artifact_id.in_(derived_artifacts),
                    )
                )
                stale_states = session.scalars(
                    select(WorkflowStageState).where(
                        WorkflowStageState.project_id == project_uuid,
                        WorkflowStageState.stage_id.in_(invalidate_stages),
                    )
                ).all()
                for stale in stale_states:
                    stale.status = "pending"
                    stale.current_run_id = None
                    stale.error_code = ""
                    stale.error_message = ""
                    stale.revision += 1
                    stale.updated_at = now
                    stored[stale.stage_id] = {
                        "status": "pending",
                        "revision": stale.revision,
                    }
            project.stage_states = stored
            project.current_stage = current_user_stage(
                {
                    key: (
                        value.get("status", "pending")
                        if isinstance(value, dict)
                        else str(value)
                    )
                    for key, value in stored.items()
                }
            )
            project.updated_at = now
            session.flush()
            return self._stage_record(state)

    def create_or_get_job(
        self,
        user_id: str,
        project_id: str | None,
        scope: str,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        retry_of_job_id: str | None = None,
        operation_key: str = "",
    ) -> JobRecord:
        user_uuid = self._uuid(user_id, not_found_message="User not found.")
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"library", "project"}:
            raise WorkflowValidationError("Job scope must be library or project.")
        normalized_idempotency_key = str(idempotency_key or "").strip()
        if not normalized_idempotency_key:
            raise WorkflowValidationError("An idempotency key is required.")
        if len(normalized_idempotency_key) > 255:
            raise WorkflowValidationError("The idempotency key is too long.")
        if normalized_scope == "library" and project_id is not None:
            raise WorkflowValidationError("Library jobs cannot reference a project.")
        if normalized_scope == "project" and project_id is None:
            raise WorkflowValidationError("Project jobs require a project.")
        normalized_job_type = str(job_type or "").strip()
        if not normalized_job_type:
            raise WorkflowValidationError("A job type is required.")
        project_uuid = (
            self._uuid(project_id, not_found_message="Project not found.") if project_id else None
        )
        retry_uuid = (
            self._uuid(retry_of_job_id, not_found_message="Retry source job not found.")
            if retry_of_job_id
            else None
        )
        base_scope_key = str(project_uuid) if project_uuid is not None else "_library_"
        normalized_operation_key = str(operation_key or "").strip()
        scope_key = (
            f"{base_scope_key}:{normalized_operation_key}"
            if normalized_operation_key
            else base_scope_key
        )
        if len(scope_key) > 255:
            raise WorkflowValidationError("The job operation key is too long.")
        requested_payload = dict(payload or {})

        try:
            with database_session(self.session_factory) as session:
                existing = session.scalar(
                    select(WorkflowJob).where(
                        WorkflowJob.user_id == user_uuid,
                        WorkflowJob.idempotency_scope_key == scope_key,
                        WorkflowJob.job_type == normalized_job_type,
                        WorkflowJob.idempotency_key == normalized_idempotency_key,
                    )
                )
                if existing:
                    if dict(existing.payload_json or {}) != requested_payload:
                        raise WorkflowConflict(
                            "The idempotency key was already used with a different payload.",
                            details={"existing_job_id": str(existing.id)},
                        )
                    return self._job_record(existing)
                if project_uuid is not None and self._owned_project(
                    session, user_uuid, project_uuid
                ) is None:
                    raise WorkflowNotFound("Project not found.")
                if retry_uuid is not None:
                    retry_source = session.scalar(
                        select(WorkflowJob).where(
                            WorkflowJob.id == retry_uuid,
                            WorkflowJob.user_id == user_uuid,
                        )
                    )
                    if retry_source is None:
                        raise WorkflowNotFound("Retry source job not found.")
                    if (
                        retry_source.job_type != normalized_job_type
                        or retry_source.scope != normalized_scope
                        or retry_source.project_id != project_uuid
                    ):
                        raise WorkflowValidationError(
                            "Retry source does not belong to the same workflow operation."
                        )
                pointer_query = select(WorkflowCurrentJob).where(
                    WorkflowCurrentJob.user_id == user_uuid,
                    WorkflowCurrentJob.scope_key == scope_key,
                    WorkflowCurrentJob.job_type == normalized_job_type,
                )
                if session.get_bind().dialect.name == "postgresql":
                    pointer_query = pointer_query.with_for_update()
                pointer = session.scalar(pointer_query)
                if pointer is not None:
                    current = session.get(WorkflowJob, pointer.job_id)
                    if current is not None and current.status in {
                        "queued",
                        "running",
                        "cancel_requested",
                    }:
                        raise WorkflowConflict(
                            "Another job of this type is already active for this scope.",
                            details={
                                "current_job_id": str(current.id),
                                "current_status": current.status,
                            },
                        )
                job = WorkflowJob(
                    user_id=user_uuid,
                    project_id=project_uuid,
                    scope=normalized_scope,
                    job_type=normalized_job_type,
                    status="queued",
                    idempotency_scope_key=scope_key,
                    idempotency_key=normalized_idempotency_key,
                    payload_json=requested_payload,
                    retry_of_job_id=retry_uuid,
                )
                session.add(job)
                session.flush()
                if pointer is None:
                    session.add(
                        WorkflowCurrentJob(
                            user_id=user_uuid,
                            scope_key=scope_key,
                            job_type=normalized_job_type,
                            project_id=project_uuid,
                            job_id=job.id,
                        )
                    )
                else:
                    pointer.project_id = project_uuid
                    pointer.job_id = job.id
                    pointer.updated_at = utc_now()
                session.flush()
                return self._job_record(job)
        except IntegrityError:
            with database_session(self.session_factory) as session:
                existing = session.scalar(
                    select(WorkflowJob).where(
                        WorkflowJob.user_id == user_uuid,
                        WorkflowJob.idempotency_scope_key == scope_key,
                        WorkflowJob.job_type == normalized_job_type,
                        WorkflowJob.idempotency_key == normalized_idempotency_key,
                    )
                )
                if existing:
                    if dict(existing.payload_json or {}) != requested_payload:
                        raise WorkflowConflict(
                            "The idempotency key was already used with a different payload.",
                            details={"existing_job_id": str(existing.id)},
                        )
                    return self._job_record(existing)
                current = session.scalar(
                    select(WorkflowJob)
                    .join(WorkflowCurrentJob, WorkflowCurrentJob.job_id == WorkflowJob.id)
                    .where(
                        WorkflowCurrentJob.user_id == user_uuid,
                        WorkflowCurrentJob.scope_key == scope_key,
                        WorkflowCurrentJob.job_type == normalized_job_type,
                    )
                )
                if current is not None and current.status in {
                    "queued",
                    "running",
                    "cancel_requested",
                }:
                    raise WorkflowConflict(
                        "Another job of this type is already active for this scope.",
                        details={
                            "current_job_id": str(current.id),
                            "current_status": current.status,
                        },
                    )
            raise WorkflowConflict("A conflicting workflow job already exists.")

    def get_job(self, user_id: str, job_id: str) -> JobRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Job not found.")
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        with database_session(self.session_factory) as session:
            job = session.scalar(
                select(WorkflowJob).where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.user_id == user_uuid,
                )
            )
            return self._job_record(job) if job else None

    def list_project_jobs(
        self,
        user_id: str,
        project_id: str,
        *,
        job_type: str | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        with database_session(self.session_factory) as session:
            if self._owned_project(session, user_uuid, project_uuid) is None:
                raise WorkflowNotFound("Project not found.")
            statement = select(WorkflowJob).where(
                WorkflowJob.user_id == user_uuid,
                WorkflowJob.project_id == project_uuid,
            )
            if job_type:
                statement = statement.where(WorkflowJob.job_type == str(job_type))
            rows = session.scalars(
                statement.order_by(WorkflowJob.created_at.desc()).limit(
                    max(1, min(int(limit), 500))
                )
            ).all()
            return [self._job_record(row) for row in rows]

    def get_current_job(
        self,
        user_id: str,
        *,
        scope: str,
        job_type: str,
        project_id: str | None = None,
        operation_key: str = "",
    ) -> JobRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Job not found.")
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope == "library":
            scope_key = "_library_"
        elif normalized_scope == "project" and project_id:
            scope_key = str(self._uuid(project_id, not_found_message="Project not found."))
        else:
            raise WorkflowValidationError("A valid job scope is required.")
        normalized_operation_key = str(operation_key or "").strip()
        if normalized_operation_key:
            scope_key = f"{scope_key}:{normalized_operation_key}"
        if len(scope_key) > 255:
            raise WorkflowValidationError("The job operation key is too long.")
        with database_session(self.session_factory) as session:
            job = session.scalar(
                select(WorkflowJob)
                .join(WorkflowCurrentJob, WorkflowCurrentJob.job_id == WorkflowJob.id)
                .where(
                    WorkflowCurrentJob.user_id == user_uuid,
                    WorkflowCurrentJob.scope_key == scope_key,
                    WorkflowCurrentJob.job_type == str(job_type),
                )
            )
            return self._job_record(job) if job else None

    def create_artifact(
        self,
        user_id: str,
        project_id: str,
        *,
        logical_name: str,
        artifact_type: str,
        relative_path: str,
        content_sha256: str,
        lineage_sha256: str = "",
        size_bytes: int,
        mtime_ns: int,
        producer_stage: str,
        producer_run_id: str | None = None,
        availability: str = "available",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        run_uuid = (
            self._uuid(producer_run_id, not_found_message="Stage run not found.")
            if producer_run_id
            else None
        )
        with database_session(self.session_factory) as session:
            if self._owned_project(session, user_uuid, project_uuid) is None:
                raise WorkflowNotFound("Project not found.")
            if run_uuid is not None:
                owned_run = session.scalar(
                    select(WorkflowStageRun.id).where(
                        WorkflowStageRun.id == run_uuid,
                        WorkflowStageRun.project_id == project_uuid,
                    )
                )
                if owned_run is None:
                    raise WorkflowNotFound("Stage run not found.")
            artifact = WorkflowArtifact(
                project_id=project_uuid,
                logical_name=str(logical_name),
                artifact_type=str(artifact_type),
                relative_path=str(relative_path),
                content_sha256=str(content_sha256),
                lineage_sha256=str(lineage_sha256),
                size_bytes=max(0, int(size_bytes)),
                mtime_ns=max(0, int(mtime_ns)),
                availability=str(availability),
                producer_stage=str(producer_stage),
                producer_run_id=run_uuid,
                metadata_json=dict(metadata or {}),
            )
            session.add(artifact)
            session.flush()
            return self._artifact_record(artifact)

    def publish_artifact(
        self,
        *,
        user_id: str,
        project_id: str,
        artifact_id: str,
        logical_name: str,
        artifact_type: str,
        relative_path: str,
        content_sha256: str,
        lineage_sha256: str = "",
        size_bytes: int,
        mtime_ns: int,
        producer_stage: str,
        producer_run_id: str | None,
        metadata: dict[str, Any] | None = None,
        make_current: bool = True,
    ) -> ArtifactRecord:
        """Insert an immutable artifact and update its current pointer atomically."""

        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        artifact_uuid = self._uuid(artifact_id, not_found_message="Artifact ID is invalid.")
        run_uuid = (
            self._uuid(producer_run_id, not_found_message="Stage run not found.")
            if producer_run_id
            else None
        )
        try:
            with database_session(self.session_factory) as session:
                if self._owned_project(session, user_uuid, project_uuid) is None:
                    raise WorkflowNotFound("Project not found.")
                if run_uuid is not None:
                    owned_run = session.scalar(
                        select(WorkflowStageRun.id).where(
                            WorkflowStageRun.id == run_uuid,
                            WorkflowStageRun.project_id == project_uuid,
                            WorkflowStageRun.requested_by_user_id == user_uuid,
                        )
                    )
                    if owned_run is None:
                        raise WorkflowNotFound("Stage run not found.")
                artifact = WorkflowArtifact(
                    id=artifact_uuid,
                    project_id=project_uuid,
                    logical_name=logical_name,
                    artifact_type=artifact_type,
                    relative_path=relative_path,
                    content_sha256=content_sha256,
                    lineage_sha256=lineage_sha256,
                    size_bytes=max(0, int(size_bytes)),
                    mtime_ns=max(0, int(mtime_ns)),
                    availability="available",
                    producer_stage=producer_stage,
                    producer_run_id=run_uuid,
                    metadata_json=dict(metadata or {}),
                )
                session.add(artifact)
                session.flush()
                if make_current:
                    self._upsert_current_artifact(
                        session,
                        project_id=project_uuid,
                        logical_name=logical_name,
                        artifact_id=artifact_uuid,
                    )
                session.flush()
                return self._artifact_record(artifact)
        except IntegrityError as exc:
            raise WorkflowConflict(
                "Artifact publication conflicted with an existing immutable version."
            ) from exc

    def get_artifact(self, user_id: str, artifact_id: str) -> OwnedArtifactRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Artifact not found.")
        artifact_uuid = self._uuid(artifact_id, not_found_message="Artifact not found.")
        with database_session(self.session_factory) as session:
            row = session.execute(
                select(WorkflowArtifact, Project.slug)
                .join(Project, Project.id == WorkflowArtifact.project_id)
                .where(
                    WorkflowArtifact.id == artifact_uuid,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
            ).one_or_none()
            if row is None:
                return None
            artifact, project_slug = row
            return OwnedArtifactRecord(
                artifact=self._artifact_record(artifact),
                project_slug=str(project_slug),
            )

    def set_current_artifact(
        self,
        user_id: str,
        project_id: str,
        logical_name: str,
        artifact_id: str,
    ) -> ArtifactRecord:
        user_uuid = self._uuid(user_id, not_found_message="Artifact not found.")
        project_uuid = self._uuid(project_id, not_found_message="Artifact not found.")
        artifact_uuid = self._uuid(artifact_id, not_found_message="Artifact not found.")
        with database_session(self.session_factory) as session:
            if self._owned_project(session, user_uuid, project_uuid) is None:
                raise WorkflowNotFound("Artifact not found.")
            artifact = session.scalar(
                select(WorkflowArtifact).where(
                    WorkflowArtifact.id == artifact_uuid,
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.logical_name == logical_name,
                )
            )
            if artifact is None:
                raise WorkflowNotFound("Artifact not found.")
            self._upsert_current_artifact(
                session,
                project_id=project_uuid,
                logical_name=logical_name,
                artifact_id=artifact_uuid,
            )
            session.flush()
            return self._artifact_record(artifact)

    @staticmethod
    def _upsert_current_artifact(
        session,
        *,
        project_id: uuid.UUID,
        logical_name: str,
        artifact_id: uuid.UUID,
    ) -> None:
        """Atomically create or replace a logical artifact pointer."""

        values = {
            "project_id": project_id,
            "logical_name": logical_name,
            "artifact_id": artifact_id,
            "updated_at": utc_now(),
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(WorkflowCurrentArtifact).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["project_id", "logical_name"],
                    set_={
                        "artifact_id": statement.excluded.artifact_id,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
            return
        if dialect == "sqlite":
            statement = sqlite_insert(WorkflowCurrentArtifact).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["project_id", "logical_name"],
                    set_={
                        "artifact_id": statement.excluded.artifact_id,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
            return

        pointer = session.get(
            WorkflowCurrentArtifact,
            {"project_id": project_id, "logical_name": logical_name},
        )
        if pointer is None:
            session.add(WorkflowCurrentArtifact(**values))
        else:
            pointer.artifact_id = artifact_id
            pointer.updated_at = values["updated_at"]

    def get_current_artifact(
        self, user_id: str, project_id: str, logical_name: str
    ) -> ArtifactRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Artifact not found.")
        project_uuid = self._uuid(project_id, not_found_message="Artifact not found.")
        with database_session(self.session_factory) as session:
            artifact = session.scalar(
                select(WorkflowArtifact)
                .join(
                    WorkflowCurrentArtifact,
                    WorkflowCurrentArtifact.artifact_id == WorkflowArtifact.id,
                )
                .join(Project, Project.id == WorkflowArtifact.project_id)
                .where(
                    WorkflowCurrentArtifact.project_id == project_uuid,
                    WorkflowCurrentArtifact.logical_name == logical_name,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
            )
            return self._artifact_record(artifact) if artifact else None

    def list_artifacts(
        self,
        user_id: str,
        project_id: str,
        logical_name: str,
    ) -> list[ArtifactRecord]:
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        with database_session(self.session_factory) as session:
            if self._owned_project(session, user_uuid, project_uuid) is None:
                raise WorkflowNotFound("Project not found.")
            artifacts = session.scalars(
                select(WorkflowArtifact)
                .where(
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.logical_name == str(logical_name),
                    WorkflowArtifact.availability == "available",
                )
                .order_by(WorkflowArtifact.created_at.desc(), WorkflowArtifact.id.desc())
            ).all()
            return [self._artifact_record(artifact) for artifact in artifacts]

    def get_artifact_by_content(
        self,
        user_id: str,
        project_id: str,
        logical_name: str,
        content_sha256: str,
        lineage_sha256: str = "",
    ) -> ArtifactRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Artifact not found.")
        project_uuid = self._uuid(project_id, not_found_message="Artifact not found.")
        with database_session(self.session_factory) as session:
            artifact = session.scalar(
                select(WorkflowArtifact)
                .join(Project, Project.id == WorkflowArtifact.project_id)
                .where(
                    WorkflowArtifact.project_id == project_uuid,
                    WorkflowArtifact.logical_name == logical_name,
                    WorkflowArtifact.content_sha256 == content_sha256,
                    WorkflowArtifact.lineage_sha256 == lineage_sha256,
                    Project.user_id == user_uuid,
                    Project.deleted_at.is_(None),
                )
            )
            return self._artifact_record(artifact) if artifact else None

    def record_approval(
        self,
        user_id: str,
        project_id: str,
        stage_id: str,
        *,
        subject_type: str,
        subject_id: str,
        decision: str,
        details: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        user_uuid = self._uuid(user_id, not_found_message="Project not found.")
        project_uuid = self._uuid(project_id, not_found_message="Project not found.")
        with database_session(self.session_factory) as session:
            if self._owned_project(session, user_uuid, project_uuid) is None:
                raise WorkflowNotFound("Project not found.")
            approval = WorkflowApproval(
                project_id=project_uuid,
                stage_id=stage_id,
                subject_type=str(subject_type),
                subject_id=str(subject_id),
                decision=str(decision),
                decided_by_user_id=user_uuid,
                details_json=dict(details or {}),
            )
            session.add(approval)
            session.flush()
            return self._approval_record(approval)

    def upsert_migration(
        self,
        source_kind: str,
        source_identity: str,
        *,
        source_sha256: str,
        status: str,
        report: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> MigrationRecord:
        with database_session(self.session_factory) as session:
            migration = session.scalar(
                select(WorkflowMigration).where(
                    WorkflowMigration.source_kind == source_kind,
                    WorkflowMigration.source_identity == source_identity,
                )
            )
            if migration is None:
                migration = WorkflowMigration(
                    source_kind=str(source_kind),
                    source_identity=str(source_identity),
                    source_sha256=str(source_sha256),
                    status=str(status),
                    report_json=dict(report or {}),
                    error_message=str(error_message),
                )
                session.add(migration)
            else:
                migration.source_sha256 = str(source_sha256)
                migration.status = str(status)
                migration.report_json = dict(report or {})
                migration.error_message = str(error_message)
            if status in {"succeeded", "failed"}:
                migration.finished_at = utc_now()
            session.flush()
            return self._migration_record(migration)

    def claim_job(self, job_id: str) -> JobRecord | None:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        now = utc_now()
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(WorkflowJob.id == job_uuid, WorkflowJob.status == "queued")
                .values(status="running", started_at=now, updated_at=now)
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def list_queued_jobs(self, job_types: set[str] | None = None) -> list[JobRecord]:
        with database_session(self.session_factory) as session:
            query = select(WorkflowJob).where(WorkflowJob.status == "queued")
            if job_types is not None:
                if not job_types:
                    return []
                query = query.where(WorkflowJob.job_type.in_(sorted(job_types)))
            jobs = session.scalars(query.order_by(WorkflowJob.created_at.asc())).all()
            return [self._job_record(job) for job in jobs]

    def request_job_cancellation(self, user_id: str, job_id: str) -> JobRecord | None:
        user_uuid = self._uuid(user_id, not_found_message="Job not found.")
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        now = utc_now()
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.user_id == user_uuid,
                    WorkflowJob.status == "queued",
                )
                .values(
                    status="cancelled",
                    cancellation_requested=True,
                    updated_at=now,
                    finished_at=now,
                )
                .returning(WorkflowJob)
            )
            if job is None:
                job = session.scalar(
                    update(WorkflowJob)
                    .where(
                        WorkflowJob.id == job_uuid,
                        WorkflowJob.user_id == user_uuid,
                        WorkflowJob.status == "running",
                        WorkflowJob.cancellation_requested.is_(False),
                    )
                    .values(
                        status="cancel_requested",
                        cancellation_requested=True,
                        updated_at=now,
                    )
                    .returning(WorkflowJob)
                )
            if job is None:
                job = session.scalar(
                    select(WorkflowJob).where(
                        WorkflowJob.id == job_uuid,
                        WorkflowJob.user_id == user_uuid,
                    )
                )
            return self._job_record(job) if job else None

    def job_cancellation_requested(self, job_id: str) -> bool:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        with database_session(self.session_factory) as session:
            job = session.get(WorkflowJob, job_uuid)
            return bool(
                job
                and (
                    job.cancellation_requested
                    or job.status in {"cancel_requested", "cancelled"}
                )
            )

    def update_job_progress(
        self, job_id: str, current: int, total: int
    ) -> JobRecord | None:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        safe_total = max(0, int(total))
        safe_current = max(0, int(current))
        if safe_total:
            safe_current = min(safe_current, safe_total)
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.status == "running",
                    WorkflowJob.cancellation_requested.is_(False),
                )
                .values(
                    progress_current=safe_current,
                    progress_total=safe_total,
                    updated_at=utc_now(),
                )
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def update_job_result(
        self, job_id: str, result: dict[str, Any]
    ) -> JobRecord | None:
        """Persist an incremental result without changing terminal job state."""

        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.status.in_(("running", "cancel_requested")),
                )
                .values(result_json=dict(result or {}), updated_at=utc_now())
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def mark_job_interrupted(self, job_id: str) -> JobRecord | None:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        now = utc_now()
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.status.in_(("running", "cancel_requested")),
                )
                .values(
                    status="interrupted",
                    error_code="PROCESS_INTERRUPTED",
                    error_message="The server stopped while this job was running.",
                    updated_at=now,
                    finished_at=now,
                )
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def mark_job_succeeded(
        self, job_id: str, result: dict[str, Any] | None = None
    ) -> JobRecord | None:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        now = utc_now()
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.status.in_(("running", "cancel_requested")),
                )
                .values(
                    status="succeeded",
                    cancellation_requested=False,
                    result_json=dict(result or {}),
                    error_code="",
                    error_message="",
                    updated_at=now,
                    finished_at=now,
                )
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def mark_job_cancelled(self, job_id: str) -> JobRecord | None:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        now = utc_now()
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.status.in_(("queued", "running", "cancel_requested")),
                )
                .values(
                    status="cancelled",
                    cancellation_requested=True,
                    error_code="",
                    error_message="",
                    updated_at=now,
                    finished_at=now,
                )
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def mark_job_failed(
        self, job_id: str, *, error_code: str, error_message: str
    ) -> JobRecord | None:
        job_uuid = self._uuid(job_id, not_found_message="Job not found.")
        now = utc_now()
        with database_session(self.session_factory) as session:
            job = session.scalar(
                update(WorkflowJob)
                .where(
                    WorkflowJob.id == job_uuid,
                    WorkflowJob.status == "running",
                    WorkflowJob.cancellation_requested.is_(False),
                )
                .values(
                    status="failed",
                    error_code=str(error_code or "JOB_EXECUTION_FAILED")[:96],
                    error_message=str(error_message or "Job execution failed."),
                    updated_at=now,
                    finished_at=now,
                )
                .returning(WorkflowJob)
            )
            return self._job_record(job) if job else None

    def mark_running_jobs_interrupted(self) -> int:
        now = utc_now()
        with database_session(self.session_factory) as session:
            result = session.execute(
                update(WorkflowJob)
                .where(WorkflowJob.status.in_(("running", "cancel_requested")))
                .values(
                    status="interrupted",
                    error_code="PROCESS_INTERRUPTED",
                    error_message="The server stopped while this job was running.",
                    updated_at=now,
                    finished_at=now,
                )
            )
            return int(result.rowcount or 0)

    def set_system_state(self, key: str, value: dict[str, Any]) -> None:
        with database_session(self.session_factory) as session:
            state = session.get(WorkflowSystemState, key)
            if state is None:
                session.add(WorkflowSystemState(key=key, value_json=dict(value or {})))
            else:
                state.value_json = dict(value or {})
                state.updated_at = utc_now()

    def get_system_state(self, key: str) -> dict[str, Any] | None:
        with database_session(self.session_factory) as session:
            state = session.get(WorkflowSystemState, key)
            return dict(state.value_json or {}) if state else None

    def workflow_is_ready(self) -> bool:
        with database_session(self.session_factory) as session:
            inventory = session.get(WorkflowSystemState, "legacy_source_inventory")
            if inventory is None:
                return True
            ready = session.get(WorkflowSystemState, "workflow_ready")
            return bool(ready and str((ready.value_json or {}).get("status", "")).lower() == "ready")

    def require_workflow_ready(self) -> None:
        if not self.workflow_is_ready():
            raise WorkflowMigrationRequired()
