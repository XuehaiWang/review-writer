"""Coordinate single and batch paragraph revisions using existing job leases."""
from alembic import op
import sqlalchemy as sa

revision = "20260914_0024"
down_revision = "20260911_0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("draft_paragraph_tasks",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("paragraph_key", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False))


def downgrade():
    op.drop_table("draft_paragraph_tasks")
