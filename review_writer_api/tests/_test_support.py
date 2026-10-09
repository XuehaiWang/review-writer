"""Shared test helpers for review_writer_api tests."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.database import Base


def session_factory():
    """Return an in-memory SQLite session factory plus its engine.

    Foreign keys are enabled on every connection and all application tables
    are created before the factory is returned.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


class JobPollingMixin:
    """Mixin for TestCases that need to poll job endpoints to completion."""

    job_poll_timeout_seconds: float = 5.0

    def wait_job(self, client: TestClient, job_id: str) -> dict:
        deadline = time.monotonic() + self.job_poll_timeout_seconds
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/jobs/{job_id}").json()
            if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return payload
            time.sleep(0.02)
        self.fail("Job did not finish.")
