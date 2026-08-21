"""Add non-versioned bibliography audit state for Library papers.

Revision ID: 20260820_0013
Revises: 20260819_0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260820_0013"
down_revision = "20260819_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_bibliography_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("library_paper_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.String(length=96), nullable=False),
        sa.Column("audit_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["library_paper_id"], ["library_papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("library_paper_id", name="uq_library_bibliography_audit_paper"),
    )
    op.create_index(
        "ix_library_bibliography_audits_user_id",
        "library_bibliography_audits",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_library_bibliography_audits_user_paper",
        "library_bibliography_audits",
        ["user_id", "paper_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_library_bibliography_audits_user_paper", table_name="library_bibliography_audits")
    op.drop_index("ix_library_bibliography_audits_user_id", table_name="library_bibliography_audits")
    op.drop_table("library_bibliography_audits")
