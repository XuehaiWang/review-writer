"""Small-project SQLAlchemy models: users, sessions, projects, and API settings."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base, TimestampMixin):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_expires", "user_id", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserCreditAccount(Base, TimestampMixin):
    """Materialized account balance derived from the immutable credit ledger."""

    __tablename__ = "user_credit_accounts"
    __table_args__ = (
        CheckConstraint("reserved_usd >= 0", name="reserved_nonnegative"),
        CheckConstraint("lifetime_credited_usd >= 0", name="credited_nonnegative"),
        CheckConstraint("lifetime_debited_usd >= 0", name="debited_nonnegative"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    balance_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    lifetime_credited_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=0, nullable=False
    )
    lifetime_debited_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=0, nullable=False
    )


class CreditReservation(Base, TimestampMixin):
    """Idempotent hold around one external provider attempt."""

    __tablename__ = "credit_reservations"
    __table_args__ = (
        UniqueConstraint(
            "reference_type",
            "reference_id",
            "attempt_number",
            name="uq_credit_reservation_reference_attempt",
        ),
        Index("ix_credit_reservations_user_created", "user_id", "created_at"),
        Index("ix_credit_reservations_job_created", "job_id", "created_at"),
        CheckConstraint("amount_usd >= 0", name="amount_nonnegative"),
        CheckConstraint("settled_amount_usd >= 0", name="settled_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="SET NULL"), index=True
    )
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_number: Mapped[int] = mapped_column(default=1, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    settled_amount_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreditTransaction(Base):
    """Append-only audit ledger for every balance and reservation mutation."""

    __tablename__ = "credit_transactions"
    __table_args__ = (
        Index("ix_credit_transactions_user_created", "user_id", "created_at"),
        Index("ix_credit_transactions_job_created", "job_id", "created_at"),
        Index("ix_credit_transactions_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="SET NULL"), index=True
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credit_reservations.id", ondelete="SET NULL"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    balance_delta_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    reserved_delta_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    balance_after_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    reserved_after_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_project_user_slug"),
        Index("ix_projects_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(96), nullable=False)
    topic: Mapped[str] = mapped_column(Text, default="", nullable=False)
    taxonomy_profile: Mapped[str] = mapped_column(
        String(96), default="general_academic", nullable=False
    )
    model_tier: Mapped[str] = mapped_column(String(32), default="terra", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    current_stage: Mapped[str] = mapped_column(String(64), default="discovery", nullable=False)
    stage_states: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderCredential(Base, TimestampMixin):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "provider_kind", name="uq_provider_credential_user_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    wire_api: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_hint: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ServerProviderCredential(Base, TimestampMixin):
    """One server-wide provider configuration, encrypted at rest.

    This table deliberately has no ``user_id``: administrators manage one
    shared provider for every hosted user.  Per-user credentials remain in the
    legacy table only for migration compatibility.
    """

    __tablename__ = "server_provider_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    provider_kind: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    wire_api: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, default=b"", nullable=False)
    secret_hint: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ServerProviderAuditEvent(Base):
    """Security audit trail for server provider changes and connection tests."""

    __tablename__ = "server_provider_audit_events"
    __table_args__ = (
        Index("ix_server_provider_audit_created", "created_at"),
        Index("ix_server_provider_audit_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AIModelRequest(Base, TimestampMixin):
    """One idempotent logical model request and its metered provider result."""

    __tablename__ = "ai_model_requests"
    __table_args__ = (
        UniqueConstraint("job_id", "request_key", name="uq_ai_model_request_job_key"),
        Index("ix_ai_model_requests_user_created", "user_id", "created_at"),
        Index("ix_ai_model_requests_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(96), nullable=False)
    model_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    attempt_count: Mapped[int] = mapped_column(default=1, nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    input_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    input_price_usd_per_million: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    cached_input_price_usd_per_million: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    output_price_usd_per_million: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    provider_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIImageRequest(Base, TimestampMixin):
    """One idempotent image generation/edit request and its record-only cost."""

    __tablename__ = "ai_image_requests"
    __table_args__ = (
        UniqueConstraint("job_id", "request_key", name="uq_ai_image_request_job_key"),
        Index("ix_ai_image_requests_user_created", "user_id", "created_at"),
        Index("ix_ai_image_requests_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(96), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    attempt_count: Mapped[int] = mapped_column(default=1, nullable=False)
    provider_attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    image_count: Mapped[int] = mapped_column(default=0, nullable=False)
    unit_price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    provider_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MinerUUsageEvent(Base, TimestampMixin):
    """Canonical per-user PDF parse event with cache-hit and page metering."""

    __tablename__ = "mineru_usage_events"
    __table_args__ = (
        UniqueConstraint("user_id", "file_sha256", name="uq_mineru_usage_user_file"),
        Index("ix_mineru_usage_user_created", "user_id", "created_at"),
        Index("ix_mineru_usage_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_jobs.id", ondelete="SET NULL"), index=True
    )
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    paper_id: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    attempt_count: Mapped[int] = mapped_column(default=1, nullable=False)
    cache_hit_count: Mapped[int] = mapped_column(default=0, nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    page_count: Mapped[int] = mapped_column(default=0, nullable=False)
    billable_pages: Mapped[int] = mapped_column(default=0, nullable=False)
    unit_price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    provider_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def create_session_factory(database_url: str):
    if not str(database_url or "").strip():
        raise ValueError("A database URL is required.")
    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine


@contextmanager
def database_session(session_factory):
    session: Session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
