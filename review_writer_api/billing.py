"""Credit accounts, provider-call reservations, and append-only ledger operations."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, or_, select

from .database import (
    AIImageRequest,
    AIModelRequest,
    CreditReservation,
    CreditTransaction,
    MinerUUsageEvent,
    Project,
    User,
    UserCreditAccount,
    UserSession,
    database_session,
    utc_now,
)
from .errors import WorkflowError, WorkflowNotFound, WorkflowValidationError
from .workflow_contracts import TERMINAL_JOB_STATUSES
from .workflow_models import WorkflowJob


MONEY_QUANTUM = Decimal("0.00000001")
ZERO = Decimal("0.00000000")


class InsufficientCredit(WorkflowError):
    code = "INSUFFICIENT_CREDIT"
    status_code = 402


class CreditConflict(WorkflowError):
    code = "CREDIT_CONFLICT"
    status_code = 409


def money(value: Decimal | str | int | float) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WorkflowValidationError("金额格式无效。") from exc


def money_text(value: Decimal | str | int | float) -> str:
    return f"{money(value):.8f}"


def _uuid(value: str | uuid.UUID, *, label: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkflowValidationError(f"{label} 无效。") from exc


class BillingService:
    """Owns all balance mutations; callers never edit account rows directly."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _account(database, user_id: uuid.UUID, *, lock: bool = False) -> UserCreditAccount:
        statement = select(UserCreditAccount).where(UserCreditAccount.user_id == user_id)
        if lock:
            statement = statement.with_for_update()
        account = database.scalar(statement)
        if account is not None:
            return account
        if database.scalar(select(User.id).where(User.id == user_id)) is None:
            raise WorkflowNotFound("用户不存在。")
        account = UserCreditAccount(user_id=user_id)
        database.add(account)
        database.flush()
        return account

    @staticmethod
    def _available(account: UserCreditAccount) -> Decimal:
        return money(account.balance_usd) - money(account.reserved_usd)

    @staticmethod
    def _linked_request(
        database, reservation: CreditReservation
    ) -> AIModelRequest | AIImageRequest | None:
        """Return the metered request protected by one credit reservation."""

        try:
            reference_id = uuid.UUID(str(reservation.reference_id))
        except (ValueError, TypeError, AttributeError):
            return None
        if reservation.reference_type == "text_model":
            return database.get(AIModelRequest, reference_id)
        if reservation.reference_type == "image_model":
            return database.get(AIImageRequest, reference_id)
        return None

    def _release_terminal_job_holds(
        self,
        database,
        *,
        account: UserCreditAccount,
    ) -> int:
        """Release orphaned holds left behind after a job has already ended.

        The gateway normally releases a hold in its request-level ``except``
        block.  A process or connection can still disappear between reserving
        credit and entering that cleanup path.  Such a hold must not reduce a
        user's available balance forever.  A successfully metered request is
        deliberately left untouched so it can still be reconciled at cost.
        """

        reservations = database.scalars(
            select(CreditReservation)
            .join(WorkflowJob, WorkflowJob.id == CreditReservation.job_id)
            .where(
                CreditReservation.user_id == account.user_id,
                CreditReservation.status == "active",
                WorkflowJob.status.in_(TERMINAL_JOB_STATUSES),
            )
            .order_by(CreditReservation.created_at, CreditReservation.id)
            .with_for_update()
        ).all()
        released = 0
        now = utc_now()
        for reservation in reservations:
            linked_request = self._linked_request(database, reservation)
            if linked_request is not None and linked_request.status == "succeeded":
                continue
            held = money(reservation.amount_usd)
            account.reserved_usd = max(ZERO, money(account.reserved_usd) - held)
            account.updated_at = now
            reservation.status = "released"
            reservation.released_at = now
            reservation.updated_at = now
            if linked_request is not None and linked_request.status == "running":
                linked_request.status = "failed"
                linked_request.error_message = (
                    "The parent workflow job ended before the provider request completed."
                )
                linked_request.finished_at = now
                linked_request.updated_at = now
            if held > ZERO:
                self._append(
                    database,
                    account=account,
                    transaction_type="release",
                    idempotency_key=f"ledger:release:{reservation.id}",
                    reserved_delta=-held,
                    reservation=reservation,
                    job_id=reservation.job_id,
                    reason="任务已结束，自动释放未完成外部调用的冻结额度",
                    details={"reconciled_terminal_job": True},
                )
            released += 1
        return released

    @staticmethod
    def _append(
        database,
        *,
        account: UserCreditAccount,
        transaction_type: str,
        idempotency_key: str,
        balance_delta: Decimal = ZERO,
        reserved_delta: Decimal = ZERO,
        reservation: CreditReservation | None = None,
        job_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> CreditTransaction:
        existing = database.scalar(
            select(CreditTransaction).where(
                CreditTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
        transaction = CreditTransaction(
            user_id=account.user_id,
            job_id=job_id,
            reservation_id=reservation.id if reservation is not None else None,
            actor_user_id=actor_user_id,
            transaction_type=transaction_type,
            balance_delta_usd=money(balance_delta),
            reserved_delta_usd=money(reserved_delta),
            balance_after_usd=money(account.balance_usd),
            reserved_after_usd=money(account.reserved_usd),
            currency=account.currency,
            idempotency_key=idempotency_key,
            reason=str(reason or "").strip()[:2000],
            details_json=dict(details or {}),
        )
        database.add(transaction)
        database.flush()
        return transaction

    def account_summary(self, user_id: str | uuid.UUID) -> dict[str, Any]:
        target = _uuid(user_id, label="用户 ID")
        with database_session(self.session_factory) as database:
            account = self._account(database, target, lock=True)
            self._release_terminal_job_holds(database, account=account)
            return self._account_dict(account)

    def reserve(
        self,
        *,
        user_id: str | uuid.UUID,
        amount_usd: Decimal | str | int | float,
        reference_type: str,
        reference_id: str | uuid.UUID,
        attempt_number: int,
        job_id: str | uuid.UUID | None = None,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> CreditReservation:
        target = _uuid(user_id, label="用户 ID")
        hold = money(amount_usd)
        if hold < ZERO:
            raise WorkflowValidationError("冻结金额不能为负数。")
        normalized_type = str(reference_type or "").strip()[:32]
        normalized_reference = str(reference_id or "").strip()[:128]
        attempt = max(1, int(attempt_number))
        if not normalized_type or not normalized_reference:
            raise WorkflowValidationError("冻结记录必须关联真实的外部调用。")
        idempotency_key = f"reserve:{normalized_type}:{normalized_reference}:{attempt}"
        normalized_job_id = _uuid(job_id, label="任务 ID") if job_id else None
        with database_session(self.session_factory) as database:
            existing = database.scalar(
                select(CreditReservation).where(
                    CreditReservation.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return existing
            account = self._account(database, target, lock=True)
            self._release_terminal_job_holds(database, account=account)
            available = self._available(account)
            if available < hold:
                raise InsufficientCredit(
                    "余额不足，无法开始本次外部模型调用。",
                    details={
                        "required_usd": money_text(hold),
                        "available_usd": money_text(available),
                        "balance_usd": money_text(account.balance_usd),
                        "reserved_usd": money_text(account.reserved_usd),
                    },
                )
            reservation = CreditReservation(
                user_id=target,
                job_id=normalized_job_id,
                reference_type=normalized_type,
                reference_id=normalized_reference,
                attempt_number=attempt,
                idempotency_key=idempotency_key,
                amount_usd=hold,
                settled_amount_usd=ZERO,
                status="active",
            )
            database.add(reservation)
            database.flush()
            account.reserved_usd = money(account.reserved_usd) + hold
            account.updated_at = utc_now()
            if hold > ZERO:
                self._append(
                    database,
                    account=account,
                    transaction_type="reservation",
                    idempotency_key=f"ledger:{idempotency_key}",
                    reserved_delta=hold,
                    reservation=reservation,
                    job_id=normalized_job_id,
                    reason=reason,
                    details=details,
                )
            return reservation

    def settle(
        self,
        reservation_id: str | uuid.UUID,
        *,
        actual_usd: Decimal | str | int | float,
        details: dict[str, Any] | None = None,
    ) -> CreditReservation:
        target = _uuid(reservation_id, label="冻结记录 ID")
        actual = money(actual_usd)
        if actual < ZERO:
            raise WorkflowValidationError("实际成本不能为负数。")
        with database_session(self.session_factory) as database:
            reservation = database.scalar(
                select(CreditReservation)
                .where(CreditReservation.id == target)
                .with_for_update()
            )
            if reservation is None:
                raise WorkflowNotFound("冻结记录不存在。")
            if reservation.status in {"settled", "overrun"}:
                return reservation
            if reservation.status != "active":
                raise CreditConflict("已释放的冻结记录不能再结算。")
            account = self._account(database, reservation.user_id, lock=True)
            held = money(reservation.amount_usd)
            account.reserved_usd = max(ZERO, money(account.reserved_usd) - held)
            account.balance_usd = money(account.balance_usd) - actual
            account.lifetime_debited_usd = money(account.lifetime_debited_usd) + actual
            account.updated_at = utc_now()
            reservation.settled_amount_usd = actual
            reservation.status = "overrun" if actual > held else "settled"
            reservation.settled_at = utc_now()
            reservation.updated_at = utc_now()
            merged_details = dict(details or {})
            merged_details.update(
                {
                    "held_usd": money_text(held),
                    "actual_usd": money_text(actual),
                    "overrun_usd": money_text(max(ZERO, actual - held)),
                }
            )
            if held > ZERO or actual > ZERO:
                self._append(
                    database,
                    account=account,
                    transaction_type="settlement",
                    idempotency_key=f"ledger:settle:{reservation.id}",
                    balance_delta=-actual,
                    reserved_delta=-held,
                    reservation=reservation,
                    job_id=reservation.job_id,
                    reason="按外部服务实际用量结算",
                    details=merged_details,
                )
            return reservation

    def settle_reference(
        self,
        *,
        reference_type: str,
        reference_id: str | uuid.UUID,
        attempt_number: int,
        actual_usd: Decimal | str | int | float,
        details: dict[str, Any] | None = None,
    ) -> CreditReservation | None:
        """Reconcile a successful cached request after an interrupted API response."""

        with database_session(self.session_factory) as database:
            reservation_id = database.scalar(
                select(CreditReservation.id).where(
                    CreditReservation.reference_type == str(reference_type),
                    CreditReservation.reference_id == str(reference_id),
                    CreditReservation.attempt_number == max(1, int(attempt_number)),
                )
            )
        if reservation_id is None:
            # Historical usage rows created before credit billing are intentionally
            # not charged retroactively.
            return None
        return self.settle(reservation_id, actual_usd=actual_usd, details=details)

    def release(
        self,
        reservation_id: str | uuid.UUID,
        *,
        reason: str = "外部调用未产生费用",
        details: dict[str, Any] | None = None,
    ) -> CreditReservation:
        target = _uuid(reservation_id, label="冻结记录 ID")
        with database_session(self.session_factory) as database:
            reservation = database.scalar(
                select(CreditReservation)
                .where(CreditReservation.id == target)
                .with_for_update()
            )
            if reservation is None:
                raise WorkflowNotFound("冻结记录不存在。")
            if reservation.status != "active":
                return reservation
            account = self._account(database, reservation.user_id, lock=True)
            held = money(reservation.amount_usd)
            account.reserved_usd = max(ZERO, money(account.reserved_usd) - held)
            account.updated_at = utc_now()
            reservation.status = "released"
            reservation.released_at = utc_now()
            reservation.updated_at = utc_now()
            if held > ZERO:
                self._append(
                    database,
                    account=account,
                    transaction_type="release",
                    idempotency_key=f"ledger:release:{reservation.id}",
                    reserved_delta=-held,
                    reservation=reservation,
                    job_id=reservation.job_id,
                    reason=reason,
                    details=details,
                )
            return reservation

    def adjust(
        self,
        *,
        actor_user_id: str | uuid.UUID,
        target_user_id: str | uuid.UUID,
        amount_usd: Decimal | str | int | float,
        reason: str,
        idempotency_key: str,
    ) -> CreditTransaction:
        actor = _uuid(actor_user_id, label="管理员 ID")
        target = _uuid(target_user_id, label="用户 ID")
        amount = money(amount_usd)
        normalized_reason = str(reason or "").strip()
        normalized_key = str(idempotency_key or "").strip()[:200]
        if amount == ZERO:
            raise WorkflowValidationError("调整金额不能为 0。")
        if not normalized_reason:
            raise WorkflowValidationError("管理员调整额度时必须填写原因。")
        if not normalized_key:
            raise WorkflowValidationError("缺少幂等键。")
        ledger_key = f"ledger:admin:{normalized_key}"
        with database_session(self.session_factory) as database:
            existing = database.scalar(
                select(CreditTransaction).where(
                    CreditTransaction.idempotency_key == ledger_key
                )
            )
            if existing is not None:
                if existing.user_id != target or money(existing.balance_delta_usd) != amount:
                    raise CreditConflict("该幂等键已用于另一笔额度调整。")
                return existing
            account = self._account(database, target, lock=True)
            if amount < ZERO and self._available(account) + amount < ZERO:
                raise InsufficientCredit(
                    "扣减额度不能超过用户当前可用余额。",
                    details={"available_usd": money_text(self._available(account))},
                )
            account.balance_usd = money(account.balance_usd) + amount
            if amount > ZERO:
                account.lifetime_credited_usd = money(account.lifetime_credited_usd) + amount
            else:
                account.lifetime_debited_usd = money(account.lifetime_debited_usd) + abs(amount)
            account.updated_at = utc_now()
            return self._append(
                database,
                account=account,
                transaction_type="admin_adjustment",
                idempotency_key=ledger_key,
                balance_delta=amount,
                actor_user_id=actor,
                reason=normalized_reason,
                details={"source": "admin_console"},
            )

    def transactions(self, user_id: str | uuid.UUID, *, limit: int = 100) -> list[dict[str, Any]]:
        target = _uuid(user_id, label="用户 ID")
        bounded_limit = max(1, min(int(limit), 500))
        with database_session(self.session_factory) as database:
            rows = database.scalars(
                select(CreditTransaction)
                .where(
                    CreditTransaction.user_id == target,
                    or_(
                        CreditTransaction.balance_delta_usd != 0,
                        CreditTransaction.reserved_delta_usd != 0,
                    ),
                )
                .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
                .limit(bounded_limit)
            ).all()
            return [self._transaction_dict(row) for row in rows]

    def admin_users(self, *, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        bounded_limit = max(1, min(int(limit), 500))
        with database_session(self.session_factory) as database:
            statement = select(User).order_by(User.created_at.desc()).limit(bounded_limit)
            if normalized:
                pattern = f"%{normalized}%"
                statement = (
                    select(User)
                    .where(or_(User.email.ilike(pattern), User.display_name.ilike(pattern)))
                    .order_by(User.created_at.desc())
                    .limit(bounded_limit)
                )
            users = database.scalars(statement).all()
            user_ids = [user.id for user in users]
            project_counts = dict(
                database.execute(
                    select(Project.user_id, func.count(Project.id))
                    .where(Project.user_id.in_(user_ids), Project.deleted_at.is_(None))
                    .group_by(Project.user_id)
                ).all()
            ) if user_ids else {}
            text_costs = self._costs_by_user(database, AIModelRequest, user_ids)
            image_costs = self._costs_by_user(database, AIImageRequest, user_ids)
            mineru_costs = self._costs_by_user(database, MinerUUsageEvent, user_ids)
            items: list[dict[str, Any]] = []
            for user in users:
                account = self._account(database, user.id)
                item = {
                    "user_id": str(user.id),
                    "email": user.email,
                    "display_name": user.display_name,
                    "role": user.role,
                    "status": user.status,
                    "project_count": int(project_counts.get(user.id, 0) or 0),
                    "estimated_cost_usd": money_text(
                        text_costs.get(user.id, ZERO)
                        + image_costs.get(user.id, ZERO)
                        + mineru_costs.get(user.id, ZERO)
                    ),
                    "created_at": user.created_at,
                    "last_login_at": user.last_login_at,
                }
                item.update(self._account_dict(account))
                items.append(item)
            return items

    def update_user(
        self,
        *,
        actor_user_id: str | uuid.UUID,
        target_user_id: str | uuid.UUID,
        role: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        actor = _uuid(actor_user_id, label="管理员 ID")
        target = _uuid(target_user_id, label="用户 ID")
        if actor == target and (role == "user" or status == "disabled"):
            raise CreditConflict("不能在当前会话中降级或停用自己的管理员账户。")
        if role is not None and role not in {"user", "admin"}:
            raise WorkflowValidationError("用户角色无效。")
        if status is not None and status not in {"active", "disabled"}:
            raise WorkflowValidationError("用户状态无效。")
        with database_session(self.session_factory) as database:
            user = database.scalar(select(User).where(User.id == target).with_for_update())
            if user is None:
                raise WorkflowNotFound("用户不存在。")
            if role is not None:
                user.role = role
            if status is not None:
                user.status = status
                if status == "disabled":
                    database.query(UserSession).filter(
                        UserSession.user_id == target,
                        UserSession.revoked_at.is_(None),
                    ).update({UserSession.revoked_at: utc_now()}, synchronize_session=False)
            account = self._account(database, target)
            result = {
                "user_id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "role": user.role,
                "status": user.status,
                "created_at": user.created_at,
                "last_login_at": user.last_login_at,
            }
            result.update(self._account_dict(account))
            return result

    def admin_usage_summary(self) -> dict[str, Any]:
        with database_session(self.session_factory) as database:
            user_count = int(database.scalar(select(func.count(User.id))) or 0)
            active_user_count = int(
                database.scalar(select(func.count(User.id)).where(User.status == "active")) or 0
            )
            project_count = int(
                database.scalar(
                    select(func.count(Project.id)).where(Project.deleted_at.is_(None))
                )
                or 0
            )
            text_count, text_cost, tokens = database.execute(
                select(
                    func.count(AIModelRequest.id),
                    func.coalesce(func.sum(AIModelRequest.provider_cost_usd), 0),
                    func.coalesce(func.sum(AIModelRequest.total_tokens), 0),
                ).where(AIModelRequest.status == "succeeded")
            ).one()
            image_count, image_cost = database.execute(
                select(
                    func.coalesce(func.sum(AIImageRequest.image_count), 0),
                    func.coalesce(func.sum(AIImageRequest.provider_cost_usd), 0),
                ).where(AIImageRequest.status == "succeeded")
            ).one()
            mineru_pages, mineru_cost = database.execute(
                select(
                    func.coalesce(func.sum(MinerUUsageEvent.billable_pages), 0),
                    func.coalesce(func.sum(MinerUUsageEvent.provider_cost_usd), 0),
                ).where(MinerUUsageEvent.status == "succeeded")
            ).one()
            balance_total, reserved_total = database.execute(
                select(
                    func.coalesce(func.sum(UserCreditAccount.balance_usd), 0),
                    func.coalesce(func.sum(UserCreditAccount.reserved_usd), 0),
                )
            ).one()
            total_cost = money(text_cost) + money(image_cost) + money(mineru_cost)
            return {
                "user_count": user_count,
                "active_user_count": active_user_count,
                "project_count": project_count,
                "text_request_count": int(text_count or 0),
                "total_tokens": int(tokens or 0),
                "image_count": int(image_count or 0),
                "mineru_billable_pages": int(mineru_pages or 0),
                "estimated_text_cost_usd": money_text(text_cost),
                "estimated_image_cost_usd": money_text(image_cost),
                "estimated_mineru_cost_usd": money_text(mineru_cost),
                "estimated_cost_usd": money_text(total_cost),
                "account_balance_total_usd": money_text(balance_total),
                "reserved_total_usd": money_text(reserved_total),
            }

    @staticmethod
    def _costs_by_user(database, model, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        if not user_ids:
            return {}
        return {
            user_id: money(cost)
            for user_id, cost in database.execute(
                select(model.user_id, func.coalesce(func.sum(model.provider_cost_usd), 0))
                .where(model.user_id.in_(user_ids), model.status == "succeeded")
                .group_by(model.user_id)
            ).all()
        }

    @staticmethod
    def _account_dict(account: UserCreditAccount) -> dict[str, Any]:
        return {
            "currency": account.currency,
            "balance_usd": money_text(account.balance_usd),
            "reserved_usd": money_text(account.reserved_usd),
            "available_usd": money_text(
                money(account.balance_usd) - money(account.reserved_usd)
            ),
            "lifetime_credited_usd": money_text(account.lifetime_credited_usd),
            "lifetime_debited_usd": money_text(account.lifetime_debited_usd),
            "billing_mode": "credit",
            "updated_at": account.updated_at,
        }

    @staticmethod
    def _transaction_dict(row: CreditTransaction) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "user_id": str(row.user_id),
            "job_id": str(row.job_id) if row.job_id else None,
            "reservation_id": str(row.reservation_id) if row.reservation_id else None,
            "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
            "transaction_type": row.transaction_type,
            "balance_delta_usd": money_text(row.balance_delta_usd),
            "reserved_delta_usd": money_text(row.reserved_delta_usd),
            "balance_after_usd": money_text(row.balance_after_usd),
            "reserved_after_usd": money_text(row.reserved_after_usd),
            "currency": row.currency,
            "reason": row.reason,
            "details": dict(row.details_json or {}),
            "created_at": row.created_at,
        }
