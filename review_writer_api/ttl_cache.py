"""Small thread-safe, bounded TTL cache for process-local derived values."""
from collections import OrderedDict
import threading
import time


class TTLCache:
    def __init__(self, capacity=256, ttl=600):
        self.capacity, self.ttl = capacity, ttl
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self):
        now = time.monotonic()
        for key in [k for k, (expiry, _) in self._items.items() if expiry <= now]:
            del self._items[key]

    def get(self, key, default=None):
        with self._lock:
            self._expire()
            item = self._items.get(key)
            if item is None:
                return default
            self._items.move_to_end(key)
            return item[1]

    def __setitem__(self, key, value):
        with self._lock:
            self._expire()
            self._items[key] = (time.monotonic() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

