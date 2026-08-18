"""Add server-wide provider configuration and audit trail.

Revision ID: 20260818_0011
Revises: 20260818_0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260818_0011"
down_revision = "20260818_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_provider_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_kind", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("wire_api", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("secret_hint", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("encryption_key_version", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_kind", name="uq_server_provider_credentials_provider_kind"),
    )
    op.create_table(
        "server_provider_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("provider_kind", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_server_provider_audit_created",
        "server_provider_audit_events",
        ["created_at"],
    )
    op.create_index(
        "ix_server_provider_audit_actor_created",
        "server_provider_audit_events",
        ["actor_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_server_provider_audit_actor_created",
        table_name="server_provider_audit_events",
    )
    op.drop_index(
        "ix_server_provider_audit_created",
        table_name="server_provider_audit_events",
    )
    op.drop_table("server_provider_audit_events")
    op.drop_table("server_provider_credentials")
