from __future__ import annotations

import unittest
import uuid
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.billing import BillingService, CreditConflict, InsufficientCredit
from review_writer_api.database import (
    Base,
    CreditReservation,
    CreditTransaction,
    User,
    database_session,
)
import review_writer_api.workflow_models  # noqa: F401 - registers FK targets


class BillingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.admin_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        with database_session(self.sessions) as database:
            database.add(
                User(
                    id=self.admin_id,
                    email="admin@example.com",
                    display_name="Admin",
                    password_hash="test",
                    role="admin",
                )
            )
            database.add(
                User(
                    id=self.user_id,
                    email="user@example.com",
                    display_name="User",
                    password_hash="test",
                )
            )
        self.billing = BillingService(self.sessions)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_adjust_reserve_settle_and_release_are_idempotent(self) -> None:
        first_adjustment = self.billing.adjust(
            actor_user_id=self.admin_id,
            target_user_id=self.user_id,
            amount_usd="10",
            reason="Initial testing credit",
            idempotency_key="adjust-1",
        )
        repeated_adjustment = self.billing.adjust(
            actor_user_id=self.admin_id,
            target_user_id=self.user_id,
            amount_usd="10",
            reason="Initial testing credit",
            idempotency_key="adjust-1",
        )
        self.assertEqual(first_adjustment.id, repeated_adjustment.id)

        reservation = self.billing.reserve(
            user_id=self.user_id,
            amount_usd="3",
            reference_type="text_model",
            reference_id="request-1",
            attempt_number=1,
            reason="Text request",
        )
        repeated_reservation = self.billing.reserve(
            user_id=self.user_id,
            amount_usd="3",
            reference_type="text_model",
            reference_id="request-1",
            attempt_number=1,
            reason="Text request",
        )
        self.assertEqual(reservation.id, repeated_reservation.id)
        self.assertEqual("7.00000000", self.billing.account_summary(self.user_id)["available_usd"])

        self.billing.settle(reservation.id, actual_usd="2.25")
        self.billing.settle(reservation.id, actual_usd="2.25")
        summary = self.billing.account_summary(self.user_id)
        self.assertEqual("7.75000000", summary["balance_usd"])
        self.assertEqual("0.00000000", summary["reserved_usd"])

        release_reservation = self.billing.reserve(
            user_id=self.user_id,
            amount_usd="1.5",
            reference_type="image_model",
            reference_id="request-2",
            attempt_number=1,
        )
        self.billing.release(release_reservation.id, reason="Provider failed")
        self.billing.release(release_reservation.id, reason="Provider failed")
        self.assertEqual("7.75000000", self.billing.account_summary(self.user_id)["available_usd"])

        with database_session(self.sessions) as database:
            reservations = database.scalars(select(CreditReservation)).all()
            transactions = database.scalars(select(CreditTransaction)).all()
        self.assertEqual(2, len(reservations))
        self.assertEqual(5, len(transactions))

    def test_insufficient_credit_blocks_before_reservation(self) -> None:
        with self.assertRaises(InsufficientCredit) as captured:
            self.billing.reserve(
                user_id=self.user_id,
                amount_usd="0.01",
                reference_type="mineru",
                reference_id="parse-1",
                attempt_number=1,
            )
        self.assertEqual("0.00000000", captured.exception.details["available_usd"])
        self.assertEqual([], self.billing.transactions(self.user_id))

    def test_provider_overrun_is_recorded_and_blocks_future_spend(self) -> None:
        self.billing.adjust(
            actor_user_id=self.admin_id,
            target_user_id=self.user_id,
            amount_usd="1",
            reason="Small credit",
            idempotency_key="adjust-overrun",
        )
        reservation = self.billing.reserve(
            user_id=self.user_id,
            amount_usd="0.5",
            reference_type="text_model",
            reference_id="overrun-request",
            attempt_number=1,
        )
        settled = self.billing.settle(reservation.id, actual_usd="1.25")
        self.assertEqual("overrun", settled.status)
        self.assertEqual("-0.25000000", self.billing.account_summary(self.user_id)["available_usd"])
        with self.assertRaises(InsufficientCredit):
            self.billing.reserve(
                user_id=self.user_id,
                amount_usd=Decimal("0"),
                reference_type="text_model",
                reference_id="blocked-request",
                attempt_number=1,
            )

    def test_idempotency_key_cannot_be_reused_for_another_adjustment(self) -> None:
        self.billing.adjust(
            actor_user_id=self.admin_id,
            target_user_id=self.user_id,
            amount_usd="2",
            reason="Credit",
            idempotency_key="same-key",
        )
        with self.assertRaises(CreditConflict):
            self.billing.adjust(
                actor_user_id=self.admin_id,
                target_user_id=self.user_id,
                amount_usd="3",
                reason="Different credit",
                idempotency_key="same-key",
            )


if __name__ == "__main__":
    unittest.main()
