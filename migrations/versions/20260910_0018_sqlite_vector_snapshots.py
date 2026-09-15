"""Local immutable vector snapshots; default backend remains PostgreSQL."""
import sqlalchemy as sa
from alembic import op

revision = "20260910_0018"
down_revision = "20260826_0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("library_vector_stores",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("backend", sa.String(16), nullable=False, server_default="postgresql"),
        sa.Column("heads_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.Uuid()), sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.create_table("library_vector_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_key", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False), sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False), sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False), sa.Column("parent_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_library_vector_versions_user_id", "library_vector_versions", ["user_id"])
    op.create_table("library_vector_read_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("library_vector_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_library_vector_read_leases_version_id", "library_vector_read_leases", ["version_id"])
    op.create_table("library_vector_job_pins",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("workflow_jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_key", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("library_vector_versions.id", ondelete="CASCADE"), nullable=False))


def downgrade():
    op.drop_table("library_vector_job_pins")
    op.drop_table("library_vector_read_leases")
    op.drop_table("library_vector_versions")
    op.drop_table("library_vector_stores")
