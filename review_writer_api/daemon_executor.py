"""Small bounded worker pool whose threads never block interpreter exit."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any


WorkItem = tuple[Future, Callable[..., Any], tuple[Any, ...], dict[str, Any]]


class DaemonWorkerPool:
    """A minimal Future-compatible pool for process-owned background jobs.

    Python's standard ThreadPoolExecutor installs an interpreter-exit hook that
    joins running workers, even after ``shutdown(wait=False)``. Review Writer's
    real work runs in killable subprocess trees, but a defensive daemon pool
    also guarantees a faulty/non-cooperative handler cannot keep the API process
    alive forever during deployment shutdown.
    """

    def __init__(self, max_workers: int, *, thread_name_prefix: str):
        self._queue: queue.Queue[WorkItem | None] = queue.Queue()
        self._lock = threading.Lock()
        self._accepting = True
        self._threads = tuple(
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}-{index + 1}",
                daemon=True,
            )
            for index in range(max(1, int(max_workers)))
        )
        for thread in self._threads:
            thread.start()

    def submit(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Cannot schedule work after executor shutdown.")
            self._queue.put((future, function, args, kwargs))
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._accepting:
                self._accepting = False
                if cancel_futures:
                    self._cancel_queued()
                for _thread in self._threads:
                    self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

    def _cancel_queued(self) -> None:
        retained_sentinels = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                retained_sentinels += 1
            else:
                item[0].cancel()
            self._queue.task_done()
        for _ in range(retained_sentinels):
            self._queue.put(None)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, function, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()
