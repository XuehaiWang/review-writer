"""Add MinerU file/page usage ledger.

Revision ID: 20260818_0010
Revises: 20260818_0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260818_0010"
down_revision = "20260818_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mineru_usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("paper_id", sa.String(length=96), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cache_hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_request_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("billable_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit_price_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("provider_cost_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "file_sha256", name="uq_mineru_usage_user_file"),
    )
    op.create_index("ix_mineru_usage_events_job_id", "mineru_usage_events", ["job_id"])
    op.create_index(
        "ix_mineru_usage_project_created",
        "mineru_usage_events",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_mineru_usage_user_created",
        "mineru_usage_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mineru_usage_user_created", table_name="mineru_usage_events")
    op.drop_index("ix_mineru_usage_project_created", table_name="mineru_usage_events")
    op.drop_index("ix_mineru_usage_events_job_id", table_name="mineru_usage_events")
    op.drop_table("mineru_usage_events")
