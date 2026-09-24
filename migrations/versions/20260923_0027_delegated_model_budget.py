"""Keep delegated text requests inside the parent task's model budget.

Revision ID: 20260923_0027
Revises: 20260923_0026
"""

from alembic import op
import sqlalchemy as sa


revision = "20260923_0027"
down_revision = "20260923_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_model_requests", sa.Column("budget_root_job_id", sa.Uuid(), nullable=True))
    op.create_index("ix_ai_model_requests_budget_root_job_id", "ai_model_requests", ["budget_root_job_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_model_requests_budget_root_job_id", table_name="ai_model_requests")
    op.drop_column("ai_model_requests", "budget_root_job_id")
