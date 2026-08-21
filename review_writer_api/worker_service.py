"""Independent PostgreSQL worker with leases, heartbeats, and fenced writes."""

from __future__ import annotations

import logging
import random
import socket
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Mapping

from review_writer_api.daemon_executor import DaemonWorkerPool
from review_writer_api.errors import WorkflowError
from review_writer_api.job_service import (
    JobCancellationRequested,
    JobContext,
    JobHandler,
    JobLeaseLost,
    JobShutdownRequested,
)
from review_writer_api.job_lease_context import bind_job_lease
from review_writer_api.job_queues import queue_for_job_type
from review_writer_api.workflow_repository import JobRecord, WorkflowRepository


LOGGER = logging.getLogger(__name__)


class WorkerService:
    """Poll and execute persisted jobs outside the public API process."""

    def __init__(
        self,
        repository: WorkflowRepository,
        handlers: Mapping[str, JobHandler],
        *,
        max_workers: int = 2,
        poll_seconds: float = 2.0,
        lease_seconds: int = 180,
        heartbeat_seconds: float = 30.0,
        worker_id: str = "",
        queues: set[str] | None = None,
    ):
        self.repository = repository
        self.handlers = dict(handlers)
        self.queues = frozenset(
            str(item).strip().casefold()
            for item in (queues or {"scientific", "image", "ingest", "document"})
            if str(item).strip()
        )
        unsupported = self.queues.difference(
            {"scientific", "image", "ingest", "document"}
        )
        if not self.queues or unsupported:
            raise ValueError(
                "Worker queues must be selected from scientific, image, ingest, and document."
            )
        self.supported_job_types = {
            job_type
            for job_type in self.handlers
            if queue_for_job_type(job_type) in self.queues
        }
        self.max_workers = max(1, min(int(max_workers), 16))
        self.poll_seconds = max(0.25, min(float(poll_seconds), 30.0))
        self.lease_seconds = max(30, int(lease_seconds))
        self.heartbeat_seconds = max(
            2.0, min(float(heartbeat_seconds), self.lease_seconds / 3)
        )
        self.worker_id = str(worker_id or "").strip() or (
            f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
        )
        self._shutdown = threading.Event()
        self._executor = DaemonWorkerPool(
            self.max_workers, thread_name_prefix="review-writer-worker"
        )
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()

    def stop(self) -> None:
        self._shutdown.set()

    def run_forever(self) -> None:
        LOGGER.info(
            "worker_started worker_id=%s queues=%s concurrency=%s poll_seconds=%s lease_seconds=%s",
            self.worker_id,
            ",".join(sorted(self.queues)),
            self.max_workers,
            self.poll_seconds,
            self.lease_seconds,
        )
        try:
            while not self._shutdown.is_set():
                try:
                    self.repository.set_system_state(
                        f"worker_heartbeat:{self.worker_id}",
                        {
                            "status": "running",
                            "worker_id": self.worker_id,
                            "active_jobs": self._active_count(),
                            "queue_counts": self.repository.job_queue_counts(),
                        },
                    )
                except Exception as exc:
                    # An observability write must not stop task execution.
                    LOGGER.warning(
                        "worker_health_write_failed worker_id=%s exception=%s",
                        self.worker_id,
                        type(exc).__name__,
                    )
                claimed_any = False
                while not self._shutdown.is_set() and self._active_count() < self.max_workers:
                    try:
                        claimed = self.repository.claim_next_job(
                            owner=self.worker_id,
                            lease_seconds=self.lease_seconds,
                            job_types=self.supported_job_types,
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "worker_claim_failed worker_id=%s exception=%s",
                            self.worker_id,
                            type(exc).__name__,
                        )
                        self._shutdown.wait(min(5.0, self.poll_seconds * 2))
                        break
                    if claimed is None:
                        break
                    claimed_any = True
                    future = self._executor.submit(self._execute, claimed)
                    with self._lock:
                        self._futures[claimed.id] = future
                    future.add_done_callback(
                        lambda _future, job_id=claimed.id: self._forget(job_id)
                    )
                if not claimed_any:
                    delay = (
                        min(5.0, self.poll_seconds + random.uniform(0.0, 0.5))
                        if self.poll_seconds >= 2.0
                        else self.poll_seconds
                    )
                    self._shutdown.wait(delay)
        finally:
            self._shutdown.set()
            deadline = time.monotonic() + max(5.0, self.heartbeat_seconds * 2)
            while self._active_count() and time.monotonic() < deadline:
                time.sleep(0.05)
            self._executor.shutdown(wait=False, cancel_futures=True)
            try:
                self.repository.set_system_state(
                    f"worker_heartbeat:{self.worker_id}",
                    {"status": "stopped", "worker_id": self.worker_id, "active_jobs": 0},
                )
            except Exception:
                LOGGER.warning("worker_stop_health_write_failed worker_id=%s", self.worker_id)
            LOGGER.info("worker_stopped worker_id=%s", self.worker_id)

    def _active_count(self) -> int:
        with self._lock:
            return sum(1 for future in self._futures.values() if not future.done())

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _heartbeat(self, context: JobContext, stop: threading.Event) -> None:
        consecutive_failures = 0
        while not stop.wait(self.heartbeat_seconds):
            try:
                renewed = self.repository.renew_job_lease(
                    context.job_id,
                    lease_token=str(context.lease_token or ""),
                    lease_generation=context.lease_generation,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                consecutive_failures += 1
                LOGGER.warning(
                    "worker_heartbeat_failed worker_id=%s job_id=%s failures=%s exception=%s",
                    self.worker_id,
                    context.job_id,
                    consecutive_failures,
                    type(exc).__name__,
                )
                if consecutive_failures < 3:
                    continue
                context.mark_lease_lost()
                return
            if renewed is None:
                context.mark_lease_lost()
                LOGGER.warning(
                    "worker_lease_lost worker_id=%s job_id=%s generation=%s",
                    self.worker_id,
                    context.job_id,
                    context.lease_generation,
                )
                return
            consecutive_failures = 0

    def _execute(self, claimed: JobRecord) -> None:
        context = JobContext(self.repository, claimed, self._shutdown)
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(context, heartbeat_stop),
            name=f"job-heartbeat-{claimed.id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        LOGGER.info(
            "job_started worker_id=%s job_id=%s user_id=%s project_id=%s job_type=%s queue=%s generation=%s",
            self.worker_id,
            claimed.id,
            claimed.user_id,
            claimed.project_id or "",
            claimed.job_type,
            claimed.queue_name,
            claimed.lease_generation,
        )
        try:
            handler = self.handlers.get(claimed.job_type)
            if handler is None:
                self._fail(
                    context,
                    "JOB_HANDLER_NOT_REGISTERED",
                    "This job type is not available on the current worker.",
                )
                return
            context.checkpoint()
            with bind_job_lease(
                context.job_id, context.lease_token, context.lease_generation
            ):
                result = handler(context, dict(claimed.payload or {}))
            completed = self.repository.mark_job_succeeded(
                claimed.id,
                dict(result or {}),
                lease_token=context.lease_token,
                lease_generation=context.lease_generation,
            )
            if completed is not None:
                LOGGER.info("job_succeeded worker_id=%s job_id=%s", self.worker_id, claimed.id)
            elif self.repository.job_cancellation_requested(claimed.id):
                self.repository.mark_job_cancelled(
                    claimed.id,
                    lease_token=context.lease_token,
                    lease_generation=context.lease_generation,
                )
        except JobShutdownRequested:
            self.repository.release_job_lease(
                claimed.id,
                lease_token=str(context.lease_token or ""),
                lease_generation=context.lease_generation,
            )
        except JobCancellationRequested:
            self.repository.mark_job_cancelled(
                claimed.id,
                lease_token=context.lease_token,
                lease_generation=context.lease_generation,
            )
        except JobLeaseLost:
            pass
        except WorkflowError as exc:
            self._fail(context, exc.code, str(exc))
        except Exception as exc:
            LOGGER.exception(
                "job_unhandled_failure worker_id=%s job_id=%s exception=%s",
                self.worker_id,
                claimed.id,
                type(exc).__name__,
            )
            self._fail(context, "JOB_EXECUTION_FAILED", "Job execution failed.")
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)

    def _fail(self, context: JobContext, code: str, message: str) -> None:
        if self._shutdown.is_set():
            self.repository.release_job_lease(
                context.job_id,
                lease_token=str(context.lease_token or ""),
                lease_generation=context.lease_generation,
            )
            return
        if self.repository.job_cancellation_requested(context.job_id):
            self.repository.mark_job_cancelled(
                context.job_id,
                lease_token=context.lease_token,
                lease_generation=context.lease_generation,
            )
            return
        self.repository.mark_job_failed(
            context.job_id,
            error_code=code,
            error_message=message,
            lease_token=context.lease_token,
            lease_generation=context.lease_generation,
        )
