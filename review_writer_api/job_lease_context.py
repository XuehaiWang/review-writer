"""Execution-local lease context for fencing artifact and stage publication."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ActiveJobLease:
    job_id: str
    lease_token: str
    lease_generation: int


_ACTIVE_JOB_LEASE: ContextVar[ActiveJobLease | None] = ContextVar(
    "review_writer_active_job_lease", default=None
)


def active_job_lease() -> ActiveJobLease | None:
    return _ACTIVE_JOB_LEASE.get()


@contextmanager
def bind_job_lease(
    job_id: str, lease_token: str | None, lease_generation: int
) -> Iterator[None]:
    if not lease_token:
        raise RuntimeError("A claimed job is missing its fencing lease token.")
    token = _ACTIVE_JOB_LEASE.set(
        ActiveJobLease(
            job_id=str(job_id),
            lease_token=str(lease_token),
            lease_generation=int(lease_generation),
        )
    )
    try:
        yield
    finally:
        _ACTIVE_JOB_LEASE.reset(token)
