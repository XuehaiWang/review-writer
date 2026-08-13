"""Scope workflow-job idempotency by project/library and job type.

Revision ID: 20260813_0003
Revises: 20260813_0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260813_0003"
down_revision = "20260813_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_jobs",
        sa.Column(
            "idempotency_scope_key",
            sa.String(length=255),
            nullable=False,
            server_default="_library_",
        ),
    )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "UPDATE workflow_jobs SET idempotency_scope_key = "
            "CASE WHEN project_id IS NULL THEN '_library_' ELSE project_id::text END"
        )
    else:
        op.execute(
            "UPDATE workflow_jobs SET idempotency_scope_key = "
            "CASE WHEN project_id IS NULL THEN '_library_' ELSE CAST(project_id AS TEXT) END"
        )
    with op.batch_alter_table("workflow_jobs") as batch:
        batch.drop_constraint("uq_workflow_job_user_idempotency", type_="unique")
        batch.create_unique_constraint(
            "uq_workflow_job_scoped_idempotency",
            ["user_id", "idempotency_scope_key", "job_type", "idempotency_key"],
        )
        batch.alter_column("idempotency_scope_key", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("workflow_jobs") as batch:
        batch.drop_constraint("uq_workflow_job_scoped_idempotency", type_="unique")
        batch.create_unique_constraint(
            "uq_workflow_job_user_idempotency", ["user_id", "idempotency_key"]
        )
        batch.drop_column("idempotency_scope_key")
