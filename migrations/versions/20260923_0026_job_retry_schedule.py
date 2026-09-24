"""Persist delayed workflow retry eligibility.

Revision ID: 20260923_0026
Revises: 20260920_0025
"""

from alembic import op
import sqlalchemy as sa


revision = "20260923_0026"
down_revision = "20260920_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_jobs", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_jobs", sa.Column("queue_reason", sa.String(64), nullable=False, server_default=""))
    op.create_index("ix_workflow_jobs_due", "workflow_jobs", ["status", "next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_jobs_due", table_name="workflow_jobs")
    op.drop_column("workflow_jobs", "queue_reason")
    op.drop_column("workflow_jobs", "next_run_at")
