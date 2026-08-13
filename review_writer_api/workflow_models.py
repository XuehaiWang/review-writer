"""PostgreSQL-native workflow state, artifact, job, and migration models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from review_writer_api.database import Base, TimestampMixin, new_uuid, utc_now


class WorkflowSystemState(Base):
    __tablename__ = "workflow_system_state"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class LibraryPaper(Base):
    """User-owned searchable catalog for admitted, precisely parsed PDFs."""

    __tablename__ = "library_papers"
    __table_args__ = (
        UniqueConstraint("user_id", "paper_id", name="uq_library_paper_user_paper_id"),
        UniqueConstraint("user_id", "content_sha256", name="uq_library_paper_user_content"),
        Index("ix_library_papers_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paper_id: Mapped[str] = mapped_column(String(96), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    authors_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    keywords_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    tags_json: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    pdf_relative_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    markdown_relative_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowStageRun(Base):
    __tablename__ = "workflow_stage_runs"
    __table_args__ = (
        Index("ix_workflow_stage_runs_project_stage_started", "project_id", "stage_id", "started_at"),
        UniqueConstraint("legacy_id", name="uq_workflow_stage_runs_legacy_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    legacy_id: Mapped[str | None] = mapped_column(String(255))
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    output_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    input_snapshot: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    output_snapshot: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    progress_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowStageState(Base, TimestampMixin):
    __tablename__ = "workflow_stage_states"
    __table_args__ = (
        UniqueConstraint("project_id", "stage_id", name="uq_workflow_stage_state_project_stage"),
        Index("ix_workflow_stage_states_project_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_stage_runs.id", ondelete="SET NULL")
    )
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    output_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)


class WorkflowArtifact(Base):
    __tablename__ = "workflow_artifacts"
    __table_args__ = (
        UniqueConstraint("legacy_id", name="uq_workflow_artifacts_legacy_id"),
        UniqueConstraint(
            "project_id",
            "logical_name",
            "content_sha256",
            name="uq_workflow_artifact_project_logical_content",
        ),
        Index("ix_workflow_artifacts_project_type", "project_id", "artifact_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    legacy_id: Mapped[str | None] = mapped_column(String(255))
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    logical_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(96), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    availability: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    producer_stage: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    producer_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_stage_runs.id", ondelete="SET NULL")
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class WorkflowCurrentArtifact(Base):
    __tablename__ = "workflow_current_artifacts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    logical_name: Mapped[str] = mapped_column(String(1024), primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_artifacts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class WorkflowArtifactDependency(Base):
    __tablename__ = "workflow_artifact_dependencies"

    output_artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    input_artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    dependency_role: Mapped[str] = mapped_column(String(96), primary_key=True, default="input")


class WorkflowJob(Base):
    __tablename__ = "workflow_jobs"
    __table_args__ = (
        UniqueConstraint("legacy_id", name="uq_workflow_jobs_legacy_id"),
        UniqueConstraint(
            "user_id",
            "idempotency_scope_key",
            "job_type",
            "idempotency_key",
            name="uq_workflow_job_scoped_idempotency",
        ),
        Index("ix_workflow_jobs_user_status_created", "user_id", "status", "created_at"),
        Index("ix_workflow_jobs_project_type", "project_id", "job_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    legacy_id: Mapped[str | None] = mapped_column(String(255))
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    idempotency_scope_key: Mapped[str] = mapped_column(
        String(255), default="_library_", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    progress_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    retry_of_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowCurrentJob(Base):
    __tablename__ = "workflow_current_jobs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    scope_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(96), primary_key=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class WorkflowApproval(Base):
    __tablename__ = "workflow_approvals"
    __table_args__ = (
        Index("ix_workflow_approvals_project_stage_created", "project_id", "stage_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(96), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class WorkflowMigration(Base):
    __tablename__ = "workflow_migrations"
    __table_args__ = (
        UniqueConstraint("source_kind", "source_identity", name="uq_workflow_migration_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
