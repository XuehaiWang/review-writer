"""Create the compact multi-user application schema.

Revision ID: 20260811_0001
Revises: None
"""

from __future__ import annotations

from alembic import op

from review_writer_api.database import Base


revision = "20260811_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
