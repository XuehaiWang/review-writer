"""Drop projects.current_stage; the stage is derived from stage_states.

A persisted copy could only drift from the checkpoints it mirrored, and every
read already recomputes it. Values are preserved in stage_states.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260925_0029"
down_revision = "20260924_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("projects", "current_stage")


def downgrade():
    op.add_column("projects", sa.Column("current_stage", sa.String(64), nullable=False,
                                        server_default="discovery"))
