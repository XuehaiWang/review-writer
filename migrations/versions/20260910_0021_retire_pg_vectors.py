"""Retire PostgreSQL vector storage; SQLite snapshots are authoritative."""
import sqlalchemy as sa
from alembic import op

revision = "20260910_0021"
down_revision = "20260910_0020"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "library_chunk_embeddings" in sa.inspect(bind).get_table_names():
        if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM library_chunk_embeddings)")).scalar():
            raise RuntimeError("Legacy vectors remain. Verify their SQLite copies and explicitly clear the legacy rows before retiring the table.")
        op.drop_table("library_chunk_embeddings")
    # A legacy ready marker must not suppress rebuilding in the sole remaining store.
    op.execute(sa.text("""UPDATE library_document_indexes
        SET semantic_status='pending', embedding_count=0,
            semantic_error_code='', semantic_error_message=''
        WHERE semantic_status IN ('ready','building','queued') AND user_id NOT IN
            (SELECT user_id FROM library_vector_stores WHERE backend='sqlite')"""))
    with op.batch_alter_table("library_vector_stores") as batch:
        batch.drop_column("backend")
    if bind.dialect.name == "postgresql":
        op.execute("DROP EXTENSION IF EXISTS vector")


def downgrade():
    raise RuntimeError("PostgreSQL vector storage was retired permanently; restore a pre-migration backup to run older software.")
