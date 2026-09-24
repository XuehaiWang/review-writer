"""Pin the selected provider channel on each logical text request."""
from alembic import op
import sqlalchemy as sa

revision = "20260924_0028"
down_revision = "20260923_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_model_requests", sa.Column("route_json", sa.JSON(), nullable=False,
                                               server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("ai_model_requests", "route_json")
