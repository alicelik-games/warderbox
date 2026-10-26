"""P2-3: RedisBackend implements the idempotency backend protocol.

Uses a minimal fake Redis client so the test runs without the redis package.
"""

from __future__ import annotations

import fnmatch

from warderbox.idempotency import DedupeResult, IdempotencyGuard, RedisBackend


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.sets = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None):
        self.kv[k] = v

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                n += 1
            if k in self.sets:
                del self.sets[k]
                n += 1
        return n

    def sadd(self, k, *vals):
        self.sets.setdefault(k, set()).update(vals)

    def smembers(self, k):
        return set(self.sets.get(k, set()))

    def expire(self, k, ttl):
        return True

    def scan_iter(self, match):
        return [k for k in list(self.kv.keys()) if fnmatch.fnmatch(k, match)]


def test_redis_backend_dedupes():
    backend = RedisBackend(client=FakeRedis())
    guard = IdempotencyGuard(backend=backend)
    guard.add_protected_tool("charge")

    status1, _ = guard.check("charge", {"amount": 10})
    assert status1 == DedupeResult.ALLOWED
    guard.record("charge", {"amount": 10}, "ok")

    status2, cached = guard.check("charge", {"amount": 10})
    assert status2 == DedupeResult.DUPLICATE
    assert cached == "ok"


def test_redis_backend_count_and_clear():
    backend = RedisBackend(client=FakeRedis())
    guard = IdempotencyGuard(backend=backend)
    guard.add_protected_tool("charge")
    guard.record("charge", {"amount": 1}, "a")
    guard.record("charge", {"amount": 2}, "b")
    assert backend.count() == 2
    assert backend.clear("charge") == 2
    assert backend.count() == 0
