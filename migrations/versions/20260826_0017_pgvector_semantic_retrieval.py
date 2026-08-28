"""Add optional pgvector storage and semantic index state.

Revision ID: 20260826_0017
Revises: 20260821_0016

The migration deliberately remains deployable on a PostgreSQL server without
the vector extension package.  In that case the ordinary columns are added,
the vector table is skipped, and the application stays in lexical-only mode.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260826_0017"
down_revision = "20260821_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "library_document_indexes",
        sa.Column(
            "semantic_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_indexed",
        ),
    )
    op.add_column(
        "library_document_indexes",
        sa.Column(
            "embedding_profile",
            sa.String(length=64),
            nullable=False,
            server_default="retrieval_embedding",
        ),
    )
    op.add_column(
        "library_document_indexes",
        sa.Column(
            "embedding_model_snapshot",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "library_document_indexes",
        sa.Column("embedding_dimension", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "library_document_indexes",
        sa.Column("embedding_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "library_document_indexes",
        sa.Column(
            "semantic_error_code",
            sa.String(length=96),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "library_document_indexes",
        sa.Column(
            "semantic_error_message", sa.Text(), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "library_document_chunks",
        sa.Column("content_sha256", sa.String(length=64), nullable=False, server_default=""),
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    # A PL/pgSQL exception block rolls back only the attempted extension
    # creation.  This keeps the main schema migration usable when the server
    # image has not installed pgvector yet.
    op.execute(
        """
        DO $$
        BEGIN
          CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN OTHERS THEN
          RAISE NOTICE 'pgvector unavailable; semantic retrieval remains disabled: %', SQLERRM;
        END
        $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
            CREATE TABLE IF NOT EXISTS library_chunk_embeddings (
              id uuid PRIMARY KEY,
              chunk_row_id uuid NOT NULL REFERENCES library_document_chunks(id) ON DELETE CASCADE,
              user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              paper_id varchar(96) NOT NULL,
              content_sha256 varchar(64) NOT NULL,
              embedding_profile varchar(64) NOT NULL,
              embedding_model_snapshot varchar(255) NOT NULL,
              dimension integer NOT NULL,
              embedding vector NOT NULL,
              status varchar(32) NOT NULL DEFAULT 'ready',
              error_code varchar(96) NOT NULL DEFAULT '',
              error_message text NOT NULL DEFAULT '',
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_library_chunk_embedding_model
                UNIQUE (chunk_row_id, embedding_model_snapshot)
            );
            CREATE INDEX IF NOT EXISTS ix_library_chunk_embeddings_user_paper
              ON library_chunk_embeddings (user_id, paper_id);
            CREATE INDEX IF NOT EXISTS ix_library_chunk_embeddings_model_status
              ON library_chunk_embeddings
              (embedding_model_snapshot, dimension, status);
          END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TABLE IF EXISTS library_chunk_embeddings")
    op.drop_column("library_document_chunks", "content_sha256")
    op.drop_column("library_document_indexes", "semantic_error_message")
    op.drop_column("library_document_indexes", "semantic_error_code")
    op.drop_column("library_document_indexes", "embedding_count")
    op.drop_column("library_document_indexes", "embedding_dimension")
    op.drop_column("library_document_indexes", "embedding_model_snapshot")
    op.drop_column("library_document_indexes", "embedding_profile")
    op.drop_column("library_document_indexes", "semantic_status")
