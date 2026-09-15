"""Persist cancellation of remaining library uploads."""
from alembic import op
import sqlalchemy as sa

revision = "20260911_0023"
down_revision = "20260910_0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "library_upload_batch_cancellations",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), primary_key=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("library_upload_batch_cancellations")
