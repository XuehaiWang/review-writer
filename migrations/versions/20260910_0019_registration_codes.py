"""Email ownership verification before registration."""
import sqlalchemy as sa
from alembic import op

revision = "20260910_0019"
down_revision = "20260910_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registration_codes",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("code_hash", sa.String(512), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("registration_codes")
