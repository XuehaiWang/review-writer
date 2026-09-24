"""Live model admission limits shared by admin settings and the gateway."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from sqlalchemy import select

from review_writer_api.database import database_session
from review_writer_api.workflow_models import WorkflowSystemState


STATE_KEY = "model_concurrency_limits"
BOUNDS = {"text": (32, 8), "image": (8, 4), "embedding": (16, 8)}


def text_parallelism(settings=None, session_factory=None) -> int:
    """Use the gateway's live limits when scheduling independent text work."""
    if settings is not None and session_factory is not None:
        limits = load(session_factory, settings)["limits"]["text"]
        return min(limits["global"], limits["user"])
    total = int(getattr(settings, "model_gateway_max_concurrency",
                        os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_CONCURRENCY", 4)))
    user = int(getattr(settings, "model_gateway_user_concurrency",
                       os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_USER_CONCURRENCY", 4)))
    return max(1, min(total, user, BOUNDS["text"][1]))


def text_parallelism_in_session(session) -> int:
    """Read admission capacity inside the scheduler's existing transaction."""
    stored = session.get(WorkflowSystemState, STATE_KEY)
    configured = (stored.value_json or {}).get("limits") if stored else None
    if isinstance(configured, dict):
        limits = validate(configured)["text"]
        return min(limits["global"], limits["user"])
    return text_parallelism()


def defaults(settings) -> dict[str, dict[str, int]]:
    values = {
        "text": {"global": settings.model_gateway_max_concurrency,
                 "user": settings.model_gateway_user_concurrency},
        "image": {"global": settings.image_gateway_max_concurrency,
                  "user": settings.image_gateway_user_concurrency},
        "embedding": {"global": settings.embedding_gateway_max_concurrency,
                      "user": settings.embedding_gateway_user_concurrency},
    }
    for row in values.values():
        row["user"] = min(row["user"], row["global"])
    return values


def validate(value: dict) -> dict[str, dict[str, int]]:
    result = {}
    for kind, (global_max, user_max) in BOUNDS.items():
        row = value.get(kind)
        if not isinstance(row, dict):
            raise ValueError(f"{kind} concurrency settings are required.")
        total, user = row.get("global"), row.get("user")
        if (type(total) is not int or type(user) is not int
                or not 1 <= total <= global_max or not 1 <= user <= user_max
                or user > total):
            raise ValueError(f"{kind} concurrency limits are outside the allowed range.")
        result[kind] = {"global": total, "user": user}
    return result


def load(session_factory, settings) -> dict:
    with database_session(session_factory) as session:
        stored = session.get(WorkflowSystemState, STATE_KEY)
        value = dict(stored.value_json or {}) if stored else {}
    configured = value.get("limits")
    limits = validate(configured) if isinstance(configured, dict) else validate(defaults(settings))
    return {"limits": limits, "version": int(value.get("version") or 0),
            "updated_at": value.get("updated_at"), "updated_by": value.get("updated_by")}


def save(session_factory, limits: dict, actor: str) -> dict:
    validated = validate(limits)
    with database_session(session_factory) as session:
        query = select(WorkflowSystemState).where(WorkflowSystemState.key == STATE_KEY)
        if session.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        state = session.scalar(query)
        if state is None:
            state = WorkflowSystemState(key=STATE_KEY)
            session.add(state)
        previous = dict(state.value_json or {})
        value = {"limits": validated, "version": int(previous.get("version") or 0) + 1,
                 "updated_at": datetime.now(timezone.utc).isoformat(), "updated_by": actor}
        state.value_json = value
    return value


class AdjustableLimiter:
    """A limit can shrink while calls are in flight without interrupting them."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0
        self._condition = asyncio.Condition()

    async def set_limit(self, limit: int) -> None:
        async with self._condition:
            self.limit = limit
            self._condition.notify_all()

    async def __aenter__(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
        return self

    async def __aexit__(self, *_):
        async with self._condition:
            self.active -= 1
            self._condition.notify_all()
