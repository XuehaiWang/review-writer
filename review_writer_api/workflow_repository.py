"""User-scoped persistence for PostgreSQL-native workflow state and jobs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
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
    idempotency_key: str
    payload: dict[str, Any]
    result: dict[str, Any]
    progress_current: int
    progress_total: int
    cancellation_requested: bool
    error_code: str
    error_message: str
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
    size_bytes: int
    mtime_ns: int
    availability: str
    producer_stage: str
    producer_run_id: str | None
    metadata: dict[str, Any]


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
            idempotency_key=job.idempotency_key,
            payload=dict(job.payload_json or {}),
            result=dict(job.result_json or {}),
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            cancellation_requested=job.cancellation_requested,
            error_code=job.error_code,
            error_message=job.error_message,
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
            size_bytes=artifact.size_bytes,
            mtime_ns=artifact.mtime_ns,
            availability=artifact.availability,
            producer_stage=artifact.producer_stage,
            producer_run_id=str(artifact.producer_run_id) if artifact.producer_run_id else None,
            metadata=dict(artifact.metadata_json or {}),
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
                id=str(project.id), user_id=str(project.user_id), slug=project.slug
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

    def create_or_get_job(
        self,
        user_id: str,
        project_id: str | None,
        scope: str,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> JobRecord:
        user_uuid = self._uuid(user_id, not_found_message="User not found.")
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"library", "project"}:
            raise WorkflowValidationError("Job scope must be library or project.")
        if not str(idempotency_key or "").strip():
            raise WorkflowValidationError("An idempotency key is required.")
        if normalized_scope == "library" and project_id is not None:
            raise WorkflowValidationError("Library jobs cannot reference a project.")
        if normalized_scope == "project" and project_id is None:
            raise WorkflowValidationError("Project jobs require a project.")
        project_uuid = (
            self._uuid(project_id, not_found_message="Project not found.") if project_id else None
        )

        try:
            with database_session(self.session_factory) as session:
                existing = session.scalar(
                    select(WorkflowJob).where(
                        WorkflowJob.user_id == user_uuid,
                        WorkflowJob.idempotency_key == idempotency_key,
                    )
                )
                if existing:
                    return self._job_record(existing)
                if project_uuid is not None and self._owned_project(
                    session, user_uuid, project_uuid
                ) is None:
                    raise WorkflowNotFound("Project not found.")
                job = WorkflowJob(
                    user_id=user_uuid,
                    project_id=project_uuid,
                    scope=normalized_scope,
                    job_type=str(job_type or "").strip(),
                    status="queued",
                    idempotency_key=idempotency_key,
                    payload_json=dict(payload or {}),
                )
                session.add(job)
                session.flush()
                return self._job_record(job)
        except IntegrityError:
            with database_session(self.session_factory) as session:
                existing = session.scalar(
                    select(WorkflowJob).where(
                        WorkflowJob.user_id == user_uuid,
                        WorkflowJob.idempotency_key == idempotency_key,
                    )
                )
                if existing:
                    return self._job_record(existing)
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

    def create_artifact(
        self,
        user_id: str,
        project_id: str,
        *,
        logical_name: str,
        artifact_type: str,
        relative_path: str,
        content_sha256: str,
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
        size_bytes: int,
        mtime_ns: int,
        producer_stage: str,
        producer_run_id: str | None,
        metadata: dict[str, Any] | None = None,
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
                    size_bytes=max(0, int(size_bytes)),
                    mtime_ns=max(0, int(mtime_ns)),
                    availability="available",
                    producer_stage=producer_stage,
                    producer_run_id=run_uuid,
                    metadata_json=dict(metadata or {}),
                )
                session.add(artifact)
                session.flush()
                pointer = session.get(
                    WorkflowCurrentArtifact,
                    {"project_id": project_uuid, "logical_name": logical_name},
                )
                if pointer is None:
                    session.add(
                        WorkflowCurrentArtifact(
                            project_id=project_uuid,
                            logical_name=logical_name,
                            artifact_id=artifact_uuid,
                        )
                    )
                else:
                    pointer.artifact_id = artifact_uuid
                    pointer.updated_at = utc_now()
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
            pointer = session.get(
                WorkflowCurrentArtifact,
                {"project_id": project_uuid, "logical_name": logical_name},
            )
            if pointer is None:
                session.add(
                    WorkflowCurrentArtifact(
                        project_id=project_uuid,
                        logical_name=logical_name,
                        artifact_id=artifact_uuid,
                    )
                )
            else:
                pointer.artifact_id = artifact_uuid
                pointer.updated_at = utc_now()
            session.flush()
            return self._artifact_record(artifact)

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

    def mark_running_jobs_interrupted(self) -> int:
        now = utc_now()
        with database_session(self.session_factory) as session:
            result = session.execute(
                update(WorkflowJob)
                .where(WorkflowJob.status == "running")
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
