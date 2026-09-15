"""Move existing bibliography jobs to their independent worker queue."""
from alembic import op

revision = "20260910_0022"
down_revision = "20260910_0021"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE workflow_jobs SET queue_name='bibliography' WHERE job_type='library.bibliography-audit'")


def downgrade():
    op.execute("UPDATE workflow_jobs SET queue_name='ingest' WHERE job_type='library.bibliography-audit'")
