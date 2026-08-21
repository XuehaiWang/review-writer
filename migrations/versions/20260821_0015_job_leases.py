"""Add PostgreSQL worker queues, leases, heartbeats, and fencing generations.

Revision ID: 20260821_0015
Revises: 20260820_0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260821_0015"
down_revision = "20260820_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_jobs",
        sa.Column("queue_name", sa.String(length=32), nullable=False, server_default="scientific"),
    )
    op.add_column(
        "workflow_jobs",
        sa.Column("lease_owner", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column("workflow_jobs", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "workflow_jobs",
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "workflow_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workflow_jobs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workflow_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        sa.text(
            """
            UPDATE workflow_jobs
            SET queue_name = CASE
                WHEN job_type IN ('figures.redraw', 'final.overview') THEN 'image'
                WHEN job_type IN ('final.export', 'final.pdf') THEN 'document'
                WHEN job_type IN (
                    'library.upload', 'library.index', 'library.bibliography-audit',
                    'library.search', 'library.download'
                ) THEN 'ingest'
                ELSE 'scientific'
            END
            """
        )
    )
    op.create_index(
        "ix_workflow_jobs_claim",
        "workflow_jobs",
        ["queue_name", "status", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_jobs_user_queue_active",
        "workflow_jobs",
        ["user_id", "queue_name", "status", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_jobs_user_queue_active", table_name="workflow_jobs")
    op.drop_index("ix_workflow_jobs_claim", table_name="workflow_jobs")
    op.drop_column("workflow_jobs", "attempt_count")
    op.drop_column("workflow_jobs", "last_heartbeat_at")
    op.drop_column("workflow_jobs", "lease_expires_at")
    op.drop_column("workflow_jobs", "lease_generation")
    op.drop_column("workflow_jobs", "lease_token")
    op.drop_column("workflow_jobs", "lease_owner")
    op.drop_column("workflow_jobs", "queue_name")
