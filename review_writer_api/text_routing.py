"""Atomic admission across user, system and shared connection quotas."""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager


class TextChannelPool:
    def __init__(self, total: int, per_user: int):
        self.total, self.per_user = total, per_user
        self.active = 0
        self.users = Counter()
        self.connections = Counter()
        self.last_used = Counter()
        self.limits = {}
        self.sequence = 0
        self.condition = asyncio.Condition()

    async def configure(self, total: int, per_user: int):
        async with self.condition:
            self.total, self.per_user = total, per_user
            self.condition.notify_all()

    @asynccontextmanager
    async def admit(self, user_id: str, channels: list[dict]):
        if not channels:
            raise ValueError("No available model channel.")
        async with self.condition:
            for channel in channels:
                key = channel["connection_id"]
                revision = channel.get("capacity_revision", 0)
                if revision >= self.limits.get(key, (0, 0))[0]:
                    self.limits[key] = (revision, channel["max_concurrency"])
            self.condition.notify_all()
            while True:
                available = [c for c in channels if self.connections[c["connection_id"]] < self.limits[c["connection_id"]][1]]
                if self.active < self.total and self.users[user_id] < self.per_user and available:
                    channel = min(available, key=lambda c: (
                        self.connections[c["connection_id"]] / self.limits[c["connection_id"]][1],
                        self.last_used[c["connection_id"]]))
                    key = channel["connection_id"]
                    self.active += 1
                    self.users[user_id] += 1
                    self.connections[key] += 1
                    self.sequence += 1
                    self.last_used[key] = self.sequence
                    break
                await self.condition.wait()
        try:
            yield channel
        finally:
            async with self.condition:
                self.active -= 1
                self.users[user_id] -= 1
                if not self.users[user_id]:
                    del self.users[user_id]
                self.connections[key] -= 1
                self.condition.notify_all()
