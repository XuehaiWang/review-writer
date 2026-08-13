"""Distinguish immutable artifact versions by dependency lineage.

Revision ID: 20260813_0006
Revises: 20260813_0005
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "20260813_0006"
down_revision = "20260813_0005"
branch_labels = None
depends_on = None


def _fingerprint(value) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    canonical = json.dumps(
        value if isinstance(value, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def upgrade() -> None:
    op.add_column(
        "workflow_artifacts",
        sa.Column("lineage_sha256", sa.String(length=64), nullable=True),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, metadata_json FROM workflow_artifacts")
    ).mappings()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE workflow_artifacts "
                "SET lineage_sha256 = :lineage_sha256 WHERE id = :artifact_id"
            ),
            {
                "artifact_id": row["id"],
                "lineage_sha256": _fingerprint(row["metadata_json"]),
            },
        )
    with op.batch_alter_table("workflow_artifacts") as batch:
        batch.alter_column("lineage_sha256", nullable=False)
        batch.drop_constraint(
            "uq_workflow_artifact_project_logical_content", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_workflow_artifact_project_logical_content_lineage",
            ["project_id", "logical_name", "content_sha256", "lineage_sha256"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT 1 FROM workflow_artifacts "
            "GROUP BY project_id, logical_name, content_sha256 "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Cannot downgrade artifact lineage while distinct lineage versions exist."
        )
    with op.batch_alter_table("workflow_artifacts") as batch:
        batch.drop_constraint(
            "uq_workflow_artifact_project_logical_content_lineage", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_workflow_artifact_project_logical_content",
            ["project_id", "logical_name", "content_sha256"],
        )
        batch.drop_column("lineage_sha256")
