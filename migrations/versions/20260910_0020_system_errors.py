"""Persist bounded API failure metadata for administrator diagnostics."""
import sqlalchemy as sa
from alembic import op

revision = "20260910_0020"
down_revision = "20260910_0019"
branch_labels = depends_on = None


def upgrade():
    op.create_table("system_error_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("method", sa.String(12), nullable=False),
        sa.Column("route", sa.String(240), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(96), nullable=False),
        sa.Column("location", sa.String(240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_system_error_events_user_id", "system_error_events", ["user_id"])
    op.create_index("ix_system_error_events_created_at", "system_error_events", ["created_at"])


def downgrade():
    op.drop_table("system_error_events")
