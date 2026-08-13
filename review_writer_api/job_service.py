"""Small persisted executor for single-instance Review Writer deployments."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowError,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import JobRecord, WorkflowRepository


class JobCancellationRequested(Exception):
    """Internal cooperative cancellation signal; never persisted as an error."""


class JobHandler(Protocol):
    def __call__(
        self, context: "JobContext", payload: dict[str, Any]
    ) -> dict[str, Any] | None: ...


class JobContext:
    def __init__(self, repository: WorkflowRepository, job: JobRecord):
        self.repository = repository
        self.job_id = job.id
        self.user_id = job.user_id
        self.project_id = job.project_id
        self.scope = job.scope
        self.job_type = job.job_type

    def cancellation_requested(self) -> bool:
        return self.repository.job_cancellation_requested(self.job_id)

    def checkpoint(self) -> None:
        if self.cancellation_requested():
            raise JobCancellationRequested()

    def report_progress(self, current: int, total: int) -> JobRecord | None:
        self.checkpoint()
        return self.repository.update_job_progress(self.job_id, current, total)


class JobService:
    """Bounded executor whose observable state lives in PostgreSQL."""

    RETRYABLE_STATUSES = frozenset({"failed", "interrupted", "cancelled"})

    def __init__(self, repository: WorkflowRepository, *, max_workers: int = 2):
        self.repository = repository
        self.max_workers = max(1, min(int(max_workers), 16))
        self._handlers: dict[str, JobHandler] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._started = False

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        normalized = str(job_type or "").strip()
        if not normalized:
            raise WorkflowValidationError("A job type is required.")
        if not callable(handler):
            raise WorkflowValidationError("A job handler must be callable.")
        with self._lock:
            existing = self._handlers.get(normalized)
            if existing is not None and existing is not handler:
                raise WorkflowConflict(
                    "A different handler is already registered for this job type."
                )
            self._handlers[normalized] = handler

    def start(self) -> int:
        with self._lock:
            if self._started:
                return 0
            interrupted = self.repository.mark_running_jobs_interrupted()
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="review-writer-job",
            )
            self._started = True
            queued = self.repository.list_queued_jobs(set(self._handlers))
        for job in queued:
            self._schedule(job)
        return interrupted

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            executor = self._executor
            if executor is None:
                return
            self._executor = None
            self._started = False
        executor.shutdown(wait=wait, cancel_futures=True)

    def submit(
        self,
        principal: Principal,
        *,
        scope: str,
        project_id: str | None,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any] | None,
    ) -> JobRecord:
        principal.require(Permission.PROJECT_WRITE)
        normalized_type = str(job_type or "").strip()
        with self._lock:
            if normalized_type not in self._handlers:
                raise WorkflowValidationError(
                    "No executable handler is registered for this job type.",
                    details={"job_type": normalized_type},
                )
        self.start()
        job = self.repository.create_or_get_job(
            principal.user_id,
            project_id,
            scope,
            normalized_type,
            idempotency_key,
            dict(payload or {}),
        )
        if job.status == "queued":
            self._schedule(job)
        return job

    def status(self, principal: Principal, job_id: str) -> JobRecord:
        principal.require(Permission.PROJECT_READ)
        job = self.repository.get_job(principal.user_id, job_id)
        if job is None:
            raise WorkflowNotFound("Job not found.")
        return job

    def request_cancel(self, principal: Principal, job_id: str) -> JobRecord:
        principal.require(Permission.PROJECT_WRITE)
        job = self.repository.request_job_cancellation(principal.user_id, job_id)
        if job is None:
            raise WorkflowNotFound("Job not found.")
        return job

    def retry_interrupted(self, principal: Principal, job_id: str) -> JobRecord:
        principal.require(Permission.PROJECT_WRITE)
        source = self.status(principal, job_id)
        if source.status not in self.RETRYABLE_STATUSES:
            raise WorkflowConflict(
                "Only failed, interrupted, or cancelled jobs can be retried.",
                details={"status": source.status},
            )
        with self._lock:
            if source.job_type not in self._handlers:
                raise WorkflowValidationError(
                    "No executable handler is registered for this job type.",
                    details={"job_type": source.job_type},
                )
        self.start()
        retried = self.repository.create_or_get_job(
            principal.user_id,
            source.project_id,
            source.scope,
            source.job_type,
            f"retry:{source.id}:{uuid.uuid4()}",
            source.payload,
            retry_of_job_id=source.id,
        )
        self._schedule(retried)
        return retried

    def _schedule(self, job: JobRecord) -> None:
        with self._lock:
            if not self._started or self._executor is None or job.status != "queued":
                return
            if job.id in self._futures:
                return
            future = self._executor.submit(self._execute, job.id)
            self._futures[job.id] = future
            future.add_done_callback(lambda _future, job_id=job.id: self._forget(job_id))

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _execute(self, job_id: str) -> None:
        claimed = self.repository.claim_job(job_id)
        if claimed is None:
            return
        with self._lock:
            handler = self._handlers.get(claimed.job_type)
        if handler is None:
            self.repository.mark_job_failed(
                claimed.id,
                error_code="JOB_HANDLER_NOT_REGISTERED",
                error_message="This job type is not available on the current server.",
            )
            return

        context = JobContext(self.repository, claimed)
        try:
            context.checkpoint()
            result = handler(context, dict(claimed.payload or {}))
            context.checkpoint()
            completed = self.repository.mark_job_succeeded(
                claimed.id, dict(result or {})
            )
            if completed is None and context.cancellation_requested():
                self.repository.mark_job_cancelled(claimed.id)
        except JobCancellationRequested:
            self.repository.mark_job_cancelled(claimed.id)
        except WorkflowError as exc:
            if context.cancellation_requested():
                self.repository.mark_job_cancelled(claimed.id)
            else:
                self.repository.mark_job_failed(
                    claimed.id,
                    error_code=exc.code,
                    error_message=str(exc),
                )
        except Exception:
            if context.cancellation_requested():
                self.repository.mark_job_cancelled(claimed.id)
            else:
                self.repository.mark_job_failed(
                    claimed.id,
                    error_code="JOB_EXECUTION_FAILED",
                    error_message="Job execution failed.",
                )
