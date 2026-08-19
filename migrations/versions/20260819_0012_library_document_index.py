"""Add rebuildable Library document indexes and layout-aware chunks.

Revision ID: 20260819_0012
Revises: 20260818_0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260819_0012"
down_revision = "20260818_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_document_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("library_paper_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.String(length=96), nullable=False),
        sa.Column("source_lineage_json", sa.JSON(), nullable=False),
        sa.Column("source_lineage_hash", sa.String(length=64), nullable=False),
        sa.Column("chunker_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=96), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["library_paper_id"], ["library_papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "paper_id",
            "source_lineage_hash",
            "chunker_version",
            name="uq_library_document_index_lineage",
        ),
    )
    op.create_index(
        "ix_library_document_indexes_user_paper_current",
        "library_document_indexes",
        ["user_id", "paper_id", "is_current"],
    )
    op.create_index(
        "ix_library_document_indexes_status",
        "library_document_indexes",
        ["status", "updated_at"],
    )
    op.create_index(
        op.f("ix_library_document_indexes_user_id"),
        "library_document_indexes",
        ["user_id"],
    )
    op.create_table(
        "library_document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.String(length=96), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False, server_default="text"),
        sa.Column("section_path_json", sa.JSON(), nullable=False),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column("block_start", sa.Integer(), nullable=False),
        sa.Column("block_end", sa.Integer(), nullable=False),
        sa.Column("asset_refs_json", sa.JSON(), nullable=False),
        sa.Column("is_reference", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("previous_chunk_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("next_chunk_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["index_id"], ["library_document_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_id", "chunk_id", name="uq_library_document_chunk_id"),
        sa.UniqueConstraint("index_id", "ordinal", name="uq_library_document_chunk_ordinal"),
    )
    op.create_index(
        "ix_library_document_chunks_user_paper",
        "library_document_chunks",
        ["user_id", "paper_id"],
    )
    op.create_index(
        "ix_library_document_chunks_index_ordinal",
        "library_document_chunks",
        ["index_id", "ordinal"],
    )
    # PostgreSQL evaluates the same tsvector expression used by the service;
    # the stored source text remains database-portable for SQLite tests.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_library_document_chunks_fts_simple "
            "ON library_document_chunks USING gin "
            "(to_tsvector('simple'::regconfig, content))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_library_document_chunks_fts_simple")
    op.drop_index("ix_library_document_chunks_index_ordinal", table_name="library_document_chunks")
    op.drop_index("ix_library_document_chunks_user_paper", table_name="library_document_chunks")
    op.drop_table("library_document_chunks")
    op.drop_index(op.f("ix_library_document_indexes_user_id"), table_name="library_document_indexes")
    op.drop_index("ix_library_document_indexes_status", table_name="library_document_indexes")
    op.drop_index(
        "ix_library_document_indexes_user_paper_current",
        table_name="library_document_indexes",
    )
    op.drop_table("library_document_indexes")
