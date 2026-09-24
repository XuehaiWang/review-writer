from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from review_writer_api.domain_services.library import LibraryService


class _Engine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self):
        self.lock = threading.Lock()
        self.owners = {}

    def connect(self):
        return _Connection(self)


class _Connection:
    def __init__(self, engine):
        self.engine = engine

    def scalar(self, _statement, parameters):
        key = (parameters["namespace"], parameters["slot"])
        with self.engine.lock:
            if key in self.engine.owners:
                return False
            self.engine.owners[key] = self
            return True

    def execute(self, _statement, parameters):
        key = (parameters["namespace"], parameters["slot"])
        with self.engine.lock:
            assert self.engine.owners.pop(key) is self

    def commit(self):
        pass

    def close(self):
        pass

    def invalidate(self):
        pass


def test_mineru_slots_are_shared_across_service_instances():
    engine = _Engine()
    entered = [threading.Event() for _ in range(3)]
    release = threading.Event()

    def parse(index):
        service = object.__new__(LibraryService)
        service.session_factory = SimpleNamespace(kw={"bind": engine})
        service._mineru_max_concurrency = 2
        with service._mineru_slot(None):
            entered[index].set()
            release.wait(3)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(parse, index) for index in range(2)]
        try:
            assert entered[0].wait(2)
            assert entered[1].wait(2)
            futures.append(pool.submit(parse, 2))
            time.sleep(0.4)
            assert not entered[2].is_set()
        finally:
            release.set()
        assert entered[2].wait(2)
        for future in futures:
            future.result(timeout=3)
    assert not engine.owners
