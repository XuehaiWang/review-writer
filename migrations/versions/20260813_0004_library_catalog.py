"""Add the user-isolated native Library catalog.

Revision ID: 20260813_0004
Revises: 20260813_0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260813_0004"
down_revision = "20260813_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_papers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.String(length=96), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authors_json", sa.JSON(), nullable=False),
        sa.Column("keywords_json", sa.JSON(), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("pdf_relative_path", sa.String(length=2048), nullable=False),
        sa.Column("markdown_relative_path", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "paper_id", name="uq_library_paper_user_paper_id"),
        sa.UniqueConstraint("user_id", "content_sha256", name="uq_library_paper_user_content"),
    )
    op.create_index("ix_library_papers_user_id", "library_papers", ["user_id"])
    op.create_index(
        "ix_library_papers_user_updated",
        "library_papers",
        ["user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("library_papers")
