"""Add idempotent image usage ledger.

Revision ID: 20260818_0009
Revises: 20260818_0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260818_0009"
down_revision = "20260818_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_image_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("stage", sa.String(length=96), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_request_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("image_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit_price_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("provider_cost_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("response_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "request_key", name="uq_ai_image_request_job_key"),
    )
    op.create_index("ix_ai_image_requests_job_id", "ai_image_requests", ["job_id"])
    op.create_index(
        "ix_ai_image_requests_project_created",
        "ai_image_requests",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_ai_image_requests_user_created",
        "ai_image_requests",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_image_requests_user_created", table_name="ai_image_requests")
    op.drop_index("ix_ai_image_requests_project_created", table_name="ai_image_requests")
    op.drop_index("ix_ai_image_requests_job_id", table_name="ai_image_requests")
    op.drop_table("ai_image_requests")
