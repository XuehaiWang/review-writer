"""Small-project email/password authentication backed by database sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import User, UserSession, database_session, utc_now
from .security import Principal, Role


PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 256
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class AuthError(ValueError):
    pass


class AuthRateLimited(AuthError):
    pass


class AuthAttemptThrottle:
    """Small in-process sliding-window throttle for a single API instance."""

    def __init__(
        self,
        *,
        max_attempts: int = 12,
        window_seconds: int = 60,
        max_keys: int = 10_000,
        clock=time.monotonic,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self.clock = clock
        self._attempts: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, key: str) -> None:
        now = float(self.clock())
        cutoff = now - self.window_seconds
        normalized = str(key or "unknown")[:512]
        with self._lock:
            if normalized not in self._attempts and len(self._attempts) >= self.max_keys:
                self._attempts.popitem(last=False)
            attempts = [value for value in self._attempts.get(normalized, []) if value > cutoff]
            if len(attempts) >= self.max_attempts:
                self._attempts[normalized] = attempts
                raise AuthRateLimited("尝试次数过多，请稍后再试。")
            attempts.append(now)
            self._attempts[normalized] = attempts
            self._attempts.move_to_end(normalized)

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(str(key or "unknown")[:512], None)


@dataclass(frozen=True)
class AuthenticatedSession:
    principal: Principal
    token: str
    expires_at: datetime


def normalize_email(raw_email: str) -> str:
    email = str(raw_email or "").strip().casefold()
    if (
        len(email) > 320
        or "@" not in email
        or email.startswith("@")
        or email.endswith("@")
        or any(character.isspace() for character in email)
    ):
        raise AuthError("请输入有效的邮箱地址。")
    return email


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


class PasswordHasher:
    """Versioned scrypt password hashing using only Python's standard library."""

    VERSION = "scrypt-v1"

    @staticmethod
    def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=SCRYPT_DKLEN,
            maxmem=64 * 1024 * 1024,
        )

    def hash(self, password: str) -> str:
        value = str(password or "")
        if len(value) < PASSWORD_MIN_LENGTH:
            raise AuthError(f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符。")
        if len(value) > PASSWORD_MAX_LENGTH:
            raise AuthError(f"密码不能超过 {PASSWORD_MAX_LENGTH} 个字符。")
        salt = secrets.token_bytes(16)
        derived = self._derive(value, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
        return f"{self.VERSION}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_encode(salt)}${_encode(derived)}"

    def verify(self, password: str, encoded: str) -> bool:
        try:
            version, raw_n, raw_r, raw_p, raw_salt, raw_hash = str(encoded).split("$", 5)
            if version != self.VERSION:
                return False
            n, r, p = int(raw_n), int(raw_r), int(raw_p)
            if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
                return False
            expected = _decode(raw_hash)
            actual = self._derive(str(password or ""), _decode(raw_salt), n=n, r=r, p=p)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class AuthService:
    def __init__(
        self,
        session_factory,
        *,
        session_days: int = 7,
        admin_emails=(),
    ):
        self.session_factory = session_factory
        self.session_days = max(1, min(int(session_days), 30))
        self.admin_emails = frozenset(
            str(item or "").strip().casefold()
            for item in admin_emails
            if str(item or "").strip()
        )
        self.passwords = PasswordHasher()
        self._dummy_hash = self.passwords.hash("not-a-real-user-password")

    @staticmethod
    def _principal(user: User) -> Principal:
        try:
            role = Role(user.role)
        except ValueError as exc:
            raise AuthError("用户权限配置无效。") from exc
        return Principal(
            user_id=str(user.id),
            roles=frozenset({role}),
            email=user.email,
            display_name=user.display_name,
        )

    def _new_session(self, database, user: User) -> AuthenticatedSession:
        raw_token = secrets.token_urlsafe(32)
        expires_at = utc_now() + timedelta(days=self.session_days)
        database.add(
            UserSession(
                user_id=user.id,
                token_hash=_session_token_hash(raw_token),
                expires_at=expires_at,
            )
        )
        return AuthenticatedSession(
            principal=self._principal(user),
            token=raw_token,
            expires_at=expires_at,
        )

    def register(self, *, email: str, password: str, display_name: str) -> AuthenticatedSession:
        normalized_email = normalize_email(email)
        normalized_name = str(display_name or "").strip()
        if not normalized_name:
            normalized_name = normalized_email.split("@", 1)[0]
        if len(normalized_name) > 200:
            raise AuthError("显示名称不能超过 200 个字符。")
        password_hash = self.passwords.hash(password)
        with database_session(self.session_factory) as database:
            if database.scalar(select(User.id).where(User.email == normalized_email)) is not None:
                raise AuthError("该邮箱已经注册，请直接登录。")
            user = User(
                email=normalized_email,
                display_name=normalized_name,
                password_hash=password_hash,
                role=(
                    Role.ADMIN.value
                    if normalized_email in self.admin_emails
                    else Role.USER.value
                ),
                status="active",
                last_login_at=utc_now(),
            )
            database.add(user)
            try:
                database.flush()
            except IntegrityError as exc:
                raise AuthError("该邮箱已经注册，请直接登录。") from exc
            return self._new_session(database, user)

    def login(self, *, email: str, password: str) -> AuthenticatedSession:
        normalized_email = normalize_email(email)
        with database_session(self.session_factory) as database:
            user = database.scalar(select(User).where(User.email == normalized_email))
            valid_password = self.passwords.verify(
                password, user.password_hash if user is not None else self._dummy_hash
            )
            if user is None or not valid_password or user.status != "active":
                raise AuthError("邮箱或密码不正确。")
            # Bootstrap existing administrators from server configuration on
            # their next successful login. Removing an email never silently
            # demotes a database administrator.
            if normalized_email in self.admin_emails and user.role != Role.ADMIN.value:
                user.role = Role.ADMIN.value
            user.last_login_at = utc_now()
            return self._new_session(database, user)

    def resolve(self, raw_token: str) -> Principal | None:
        token_hash = _session_token_hash(raw_token)
        with database_session(self.session_factory) as database:
            row = database.execute(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .where(UserSession.token_hash == token_hash)
            ).one_or_none()
            if row is None:
                return None
            user_session, user = row
            if (
                user_session.revoked_at is not None
                or _aware(user_session.expires_at) <= datetime.now(timezone.utc)
                or user.status != "active"
            ):
                return None
            return self._principal(user)

    def logout(self, raw_token: str) -> None:
        token_hash = _session_token_hash(raw_token)
        with database_session(self.session_factory) as database:
            user_session = database.scalar(
                select(UserSession).where(UserSession.token_hash == token_hash)
            )
            if user_session is not None and user_session.revoked_at is None:
                user_session.revoked_at = utc_now()
