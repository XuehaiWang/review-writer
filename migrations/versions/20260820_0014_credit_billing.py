"""Add user credit accounts, provider-call holds, and append-only ledger.

Revision ID: 20260820_0014
Revises: 20260820_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260820_0014"
down_revision = "20260820_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_credit_accounts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("balance_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("reserved_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column(
            "lifetime_credited_usd", sa.Numeric(18, 8), nullable=False, server_default="0"
        ),
        sa.Column(
            "lifetime_debited_usd", sa.Numeric(18, 8), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_user_credit_accounts_reserved_nonnegative"),
        sa.CheckConstraint(
            "lifetime_credited_usd >= 0", name="ck_user_credit_accounts_credited_nonnegative"
        ),
        sa.CheckConstraint(
            "lifetime_debited_usd >= 0", name="ck_user_credit_accounts_debited_nonnegative"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO user_credit_accounts (
                user_id, currency, balance_usd, reserved_usd,
                lifetime_credited_usd, lifetime_debited_usd, created_at, updated_at
            )
            SELECT id, 'USD', 0, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM users
            """
        )
    )

    op.create_table(
        "credit_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("reference_type", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("amount_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("settled_amount_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_usd >= 0", name="ck_credit_reservations_amount_nonnegative"),
        sa.CheckConstraint(
            "settled_amount_usd >= 0", name="ck_credit_reservations_settled_nonnegative"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_reservations_idempotency_key"),
        sa.UniqueConstraint(
            "reference_type",
            "reference_id",
            "attempt_number",
            name="uq_credit_reservation_reference_attempt",
        ),
    )
    op.create_index(
        "ix_credit_reservations_user_id", "credit_reservations", ["user_id"], unique=False
    )
    op.create_index(
        "ix_credit_reservations_job_id", "credit_reservations", ["job_id"], unique=False
    )
    op.create_index(
        "ix_credit_reservations_user_created",
        "credit_reservations",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_credit_reservations_job_created",
        "credit_reservations",
        ["job_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "credit_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("balance_delta_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("reserved_delta_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("balance_after_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("reserved_after_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["credit_reservations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_transactions_idempotency_key"),
    )
    op.create_index(
        "ix_credit_transactions_user_id", "credit_transactions", ["user_id"], unique=False
    )
    op.create_index(
        "ix_credit_transactions_job_id", "credit_transactions", ["job_id"], unique=False
    )
    op.create_index(
        "ix_credit_transactions_reservation_id",
        "credit_transactions",
        ["reservation_id"],
        unique=False,
    )
    op.create_index(
        "ix_credit_transactions_actor_user_id",
        "credit_transactions",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_credit_transactions_user_created",
        "credit_transactions",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_credit_transactions_job_created",
        "credit_transactions",
        ["job_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_credit_transactions_actor_created",
        "credit_transactions",
        ["actor_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_credit_transactions_actor_created", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_job_created", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_user_created", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_actor_user_id", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_reservation_id", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_job_id", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_user_id", table_name="credit_transactions")
    op.drop_table("credit_transactions")
    op.drop_index("ix_credit_reservations_job_created", table_name="credit_reservations")
    op.drop_index("ix_credit_reservations_user_created", table_name="credit_reservations")
    op.drop_index("ix_credit_reservations_job_id", table_name="credit_reservations")
    op.drop_index("ix_credit_reservations_user_id", table_name="credit_reservations")
    op.drop_table("credit_reservations")
    op.drop_table("user_credit_accounts")
