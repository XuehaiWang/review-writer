"""Small persisted executor for single-instance Review Writer deployments."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, wait as wait_for_futures
from typing import Any, Protocol

from review_writer_api.daemon_executor import DaemonWorkerPool
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


class JobShutdownRequested(Exception):
    """Internal shutdown signal recorded as interrupted rather than cancelled."""


class JobHandler(Protocol):
    def __call__(
        self, context: "JobContext", payload: dict[str, Any]
    ) -> dict[str, Any] | None: ...


class JobContext:
    def __init__(
        self,
        repository: WorkflowRepository,
        job: JobRecord,
        shutdown_event: threading.Event,
    ):
        self.repository = repository
        self._shutdown_event = shutdown_event
        self.job_id = job.id
        self.user_id = job.user_id
        self.project_id = job.project_id
        self.scope = job.scope
        self.job_type = job.job_type

    def cancellation_requested(self) -> bool:
        return self.shutting_down() or self.repository.job_cancellation_requested(
            self.job_id
        )

    def shutting_down(self) -> bool:
        return self._shutdown_event.is_set()

    def checkpoint(self) -> None:
        if self.shutting_down():
            raise JobShutdownRequested()
        if self.repository.job_cancellation_requested(self.job_id):
            raise JobCancellationRequested()

    def report_progress(self, current: int, total: int) -> JobRecord | None:
        self.checkpoint()
        return self.repository.update_job_progress(self.job_id, current, total)

    def report_partial_result(self, result: dict[str, Any]) -> JobRecord | None:
        # Do not checkpoint first: a just-completed item must remain observable
        # even when cancellation arrives between artifact publication and here.
        return self.repository.update_job_result(self.job_id, result)


class JobService:
    """Bounded executor whose observable state lives in PostgreSQL."""

    RETRYABLE_STATUSES = frozenset({"failed", "interrupted", "cancelled"})

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        max_workers: int = 2,
        shutdown_grace_seconds: float = 5.0,
    ):
        self.repository = repository
        self.max_workers = max(1, min(int(max_workers), 16))
        self.shutdown_grace_seconds = max(0.0, float(shutdown_grace_seconds))
        self._handlers: dict[str, JobHandler] = {}
        self._executor: DaemonWorkerPool | None = None
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._started = False
        self._shutdown_event = threading.Event()

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
            self._shutdown_event = threading.Event()
            interrupted = self.repository.mark_running_jobs_interrupted()
            self._executor = DaemonWorkerPool(
                self.max_workers,
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
            futures = tuple(self._futures.values())
            self._shutdown_event.set()
        for future in futures:
            future.cancel()
        if wait and futures:
            wait_for_futures(futures, timeout=self.shutdown_grace_seconds)
        executor.shutdown(wait=False, cancel_futures=True)
        self.repository.mark_running_jobs_interrupted()

    def submit(
        self,
        principal: Principal,
        *,
        scope: str,
        project_id: str | None,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any] | None,
        retry_of_job_id: str | None = None,
    ) -> JobRecord:
        principal.require(Permission.PROJECT_WRITE)
        normalized_type = str(job_type or "").strip()
        with self._lock:
            if normalized_type not in self._handlers:
                raise WorkflowValidationError(
                    "No executable handler is registered for this job type.",
                    details={"job_type": normalized_type},
                )
        retry_source = None
        if retry_of_job_id:
            retry_source = self.status(principal, retry_of_job_id)
            if (
                retry_source.job_type != normalized_type
                or retry_source.scope != scope
                or retry_source.project_id != project_id
            ):
                raise WorkflowValidationError(
                    "Retry source does not belong to the same workflow operation."
                )
        self.start()
        job = self.repository.create_or_get_job(
            principal.user_id,
            project_id,
            scope,
            normalized_type,
            idempotency_key,
            dict(payload or {}),
            retry_of_job_id=retry_source.id if retry_source else None,
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
        context = JobContext(self.repository, claimed, self._shutdown_event)
        if handler is None:
            self._finish_failure(
                context,
                error_code="JOB_HANDLER_NOT_REGISTERED",
                error_message="This job type is not available on the current server.",
            )
            return

        try:
            context.checkpoint()
            result = handler(context, dict(claimed.payload or {}))
            context.checkpoint()
            completed = self.repository.mark_job_succeeded(
                claimed.id, dict(result or {})
            )
            if completed is None and context.cancellation_requested():
                if context.shutting_down():
                    self.repository.mark_job_interrupted(claimed.id)
                else:
                    self.repository.mark_job_cancelled(claimed.id)
        except JobShutdownRequested:
            self.repository.mark_job_interrupted(claimed.id)
        except JobCancellationRequested:
            self.repository.mark_job_cancelled(claimed.id)
        except WorkflowError as exc:
            self._finish_failure(
                context,
                error_code=exc.code,
                error_message=str(exc),
            )
        except Exception:
            self._finish_failure(
                context,
                error_code="JOB_EXECUTION_FAILED",
                error_message="Job execution failed.",
            )

    def _finish_failure(
        self,
        context: JobContext,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        if context.shutting_down():
            self.repository.mark_job_interrupted(context.job_id)
            return
        if self.repository.job_cancellation_requested(context.job_id):
            self.repository.mark_job_cancelled(context.job_id)
            return
        failed = self.repository.mark_job_failed(
            context.job_id,
            error_code=error_code,
            error_message=error_message,
        )
        if failed is None:
            if context.shutting_down():
                self.repository.mark_job_interrupted(context.job_id)
            elif self.repository.job_cancellation_requested(context.job_id):
                self.repository.mark_job_cancelled(context.job_id)
