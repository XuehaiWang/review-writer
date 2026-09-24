"""Store administrator-configured embedding input pricing.

Revision ID: 20260920_0025
Revises: 20260914_0024
"""

from alembic import op
import sqlalchemy as sa


revision = "20260920_0025"
down_revision = "20260914_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "server_provider_credentials",
        sa.Column("input_usd_per_million", sa.Numeric(18, 8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("server_provider_credentials", "input_usd_per_million")
