"""Add PostgreSQL-native workflow state, artifacts, jobs, and migration records.

Revision ID: 20260813_0002
Revises: 20260811_0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260813_0002"
down_revision = "20260811_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_system_state",
        sa.Column("key", sa.String(length=96), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "workflow_stage_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("legacy_id", sa.String(length=255), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("output_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("output_snapshot", sa.JSON(), nullable=False),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_id", name="uq_workflow_stage_runs_legacy_id"),
    )
    op.create_index("ix_workflow_stage_runs_project_id", "workflow_stage_runs", ["project_id"])
    op.create_index(
        "ix_workflow_stage_runs_requested_by_user_id",
        "workflow_stage_runs",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_workflow_stage_runs_project_stage_started",
        "workflow_stage_runs",
        ["project_id", "stage_id", "started_at"],
    )
    op.create_table(
        "workflow_stage_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_run_id", sa.Uuid(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("output_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_run_id"], ["workflow_stage_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "stage_id", name="uq_workflow_stage_state_project_stage"
        ),
    )
    op.create_index("ix_workflow_stage_states_project_id", "workflow_stage_states", ["project_id"])
    op.create_index(
        "ix_workflow_stage_states_project_status",
        "workflow_stage_states",
        ["project_id", "status"],
    )
    op.create_table(
        "workflow_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("legacy_id", sa.String(length=255), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("logical_name", sa.String(length=1024), nullable=False),
        sa.Column("artifact_type", sa.String(length=96), nullable=False),
        sa.Column("relative_path", sa.String(length=2048), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("producer_stage", sa.String(length=64), nullable=False),
        sa.Column("producer_run_id", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["producer_run_id"], ["workflow_stage_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_id", name="uq_workflow_artifacts_legacy_id"),
        sa.UniqueConstraint(
            "project_id",
            "logical_name",
            "content_sha256",
            name="uq_workflow_artifact_project_logical_content",
        ),
    )
    op.create_index("ix_workflow_artifacts_project_id", "workflow_artifacts", ["project_id"])
    op.create_index(
        "ix_workflow_artifacts_project_type",
        "workflow_artifacts",
        ["project_id", "artifact_type"],
    )
    op.create_table(
        "workflow_current_artifacts",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("logical_name", sa.String(length=1024), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["workflow_artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "logical_name"),
        sa.UniqueConstraint("artifact_id"),
    )
    op.create_table(
        "workflow_artifact_dependencies",
        sa.Column("output_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("input_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_role", sa.String(length=96), nullable=False),
        sa.ForeignKeyConstraint(
            ["input_artifact_id"], ["workflow_artifacts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["output_artifact_id"], ["workflow_artifacts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("output_artifact_id", "input_artifact_id", "dependency_role"),
    )
    op.create_table(
        "workflow_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("legacy_id", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("job_type", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("retry_of_job_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["retry_of_job_id"], ["workflow_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_id", name="uq_workflow_jobs_legacy_id"),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_workflow_job_user_idempotency"
        ),
    )
    op.create_index("ix_workflow_jobs_user_id", "workflow_jobs", ["user_id"])
    op.create_index("ix_workflow_jobs_project_id", "workflow_jobs", ["project_id"])
    op.create_index(
        "ix_workflow_jobs_user_status_created",
        "workflow_jobs",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_workflow_jobs_project_type", "workflow_jobs", ["project_id", "job_type"]
    )
    op.create_table(
        "workflow_current_jobs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("job_type", sa.String(length=96), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "scope_key", "job_type"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_workflow_current_jobs_project_id", "workflow_current_jobs", ["project_id"]
    )
    op.create_table(
        "workflow_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=96), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_approvals_project_id", "workflow_approvals", ["project_id"])
    op.create_index(
        "ix_workflow_approvals_decided_by_user_id",
        "workflow_approvals",
        ["decided_by_user_id"],
    )
    op.create_index(
        "ix_workflow_approvals_project_stage_created",
        "workflow_approvals",
        ["project_id", "stage_id", "created_at"],
    )
    op.create_table(
        "workflow_migrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("source_identity", sa.String(length=2048), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_kind", "source_identity", name="uq_workflow_migration_source"
        ),
    )


def downgrade() -> None:
    op.drop_table("workflow_migrations")
    op.drop_table("workflow_approvals")
    op.drop_table("workflow_current_jobs")
    op.drop_table("workflow_jobs")
    op.drop_table("workflow_artifact_dependencies")
    op.drop_table("workflow_current_artifacts")
    op.drop_table("workflow_artifacts")
    op.drop_table("workflow_stage_states")
    op.drop_table("workflow_stage_runs")
    op.drop_table("workflow_system_state")
