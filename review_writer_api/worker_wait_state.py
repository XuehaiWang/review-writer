"""Thread-local observation of scientific subprocess model waits.

The model gateway remains the owner of provider concurrency. This signal only
lets the bounded business Worker pool run another local unit while a child is
blocked on an already-admitted model request.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator


_callback: ContextVar[Callable[[bool], None] | None] = ContextVar(
    "worker_model_wait_callback", default=None
)


@contextmanager
def bind_model_wait_callback(callback: Callable[[bool], None]) -> Iterator[None]:
    token = _callback.set(callback)
    try:
        yield
    finally:
        try:
            callback(False)
        except Exception:
            pass
        _callback.reset(token)


def report_model_waiting(waiting: bool) -> None:
    callback = _callback.get()
    if callback is not None:
        try:
            callback(bool(waiting))
        except Exception:
            pass
