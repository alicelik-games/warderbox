"""Tests for idempotency guard."""

import time
from warderbox.idempotency import (
    IdempotencyGuard, DedupeResult,
    IdempotencyBackend, MemoryBackend, IdempotencyRecord,
)


def test_unprotected_tool_always_allowed():
    guard = IdempotencyGuard()
    status, cached = guard.check("tool_a", {"x": 1})
    assert status == DedupeResult.ALLOWED
    assert cached is None


def test_protected_tool_first_call_allowed():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    status, cached = guard.check("send_email", {"to": "a@b.com"})
    assert status == DedupeResult.ALLOWED


def test_duplicate_blocked():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    args = {"to": "a@b.com", "body": "hi"}
    guard.record("send_email", args, result="sent")
    status, cached = guard.check("send_email", args)
    assert status == DedupeResult.DUPLICATE
    assert cached == "sent"


def test_different_args_not_duplicate():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    guard.record("send_email", {"to": "a@b.com"}, result="sent_1")
    status, _ = guard.check("send_email", {"to": "c@d.com"})
    assert status == DedupeResult.ALLOWED


def test_ttl_expiry():
    guard = IdempotencyGuard(default_ttl=0.1)
    guard.add_protected_tool("pay")
    guard.record("pay", {"amount": 100}, result="ok")
    time.sleep(0.15)
    status, _ = guard.check("pay", {"amount": 100})
    assert status == DedupeResult.ALLOWED


def test_invalidate():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    args = {"to": "a@b.com"}
    guard.record("send_email", args, result="sent")
    assert guard.invalidate("send_email", args) is True
    status, _ = guard.check("send_email", args)
    assert status == DedupeResult.ALLOWED


def test_invalidate_nonexistent():
    guard = IdempotencyGuard()
    assert guard.invalidate("nope", {"x": 1}) is False


def test_clear_all():
    guard = IdempotencyGuard()
    guard.add_protected_tool("a")
    guard.add_protected_tool("b")
    guard.record("a", {"x": 1})
    guard.record("b", {"y": 2})
    cleared = guard.clear()
    assert cleared == 2


def test_clear_specific_tool():
    guard = IdempotencyGuard()
    guard.add_protected_tool("a")
    guard.add_protected_tool("b")
    guard.record("a", {"x": 1})
    guard.record("b", {"y": 2})
    cleared = guard.clear("a")
    assert cleared == 1
    status, _ = guard.check("b", {"y": 2})
    assert status == DedupeResult.DUPLICATE


def test_remove_protected_tool():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    assert guard.is_protected("send_email") is True
    guard.remove_protected_tool("send_email")
    assert guard.is_protected("send_email") is False


def test_stats():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    guard.record("send_email", {"to": "a@b.com"}, result="sent")
    guard.check("send_email", {"to": "a@b.com"})
    stats = guard.stats()
    assert stats["active_records"] == 1
    assert stats["duplicates_blocked"] == 1
    assert "send_email" in stats["protected_tools"]


def test_hash_deterministic():
    h1 = IdempotencyGuard._hash_args({"a": 1, "b": 2})
    h2 = IdempotencyGuard._hash_args({"b": 2, "a": 1})
    assert h1 == h2


def test_custom_ttl_per_record():
    guard = IdempotencyGuard(default_ttl=3600)
    guard.add_protected_tool("pay")
    guard.record("pay", {"amount": 50}, result="ok", ttl=0.1)
    time.sleep(0.15)
    status, _ = guard.check("pay", {"amount": 50})
    assert status == DedupeResult.ALLOWED


# --- Client-supplied idempotency keys ---

def test_client_key_dedup():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    guard.record("send_email", {"to": "a@b.com"}, result="sent", idempotency_key="order-123")
    status, cached = guard.check("send_email", {"to": "different@x.com"}, idempotency_key="order-123")
    assert status == DedupeResult.DUPLICATE
    assert cached == "sent"


def test_client_key_different_keys_allowed():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    guard.record("send_email", {}, result="sent_1", idempotency_key="order-123")
    status, _ = guard.check("send_email", {}, idempotency_key="order-456")
    assert status == DedupeResult.ALLOWED


def test_client_key_invalidate():
    guard = IdempotencyGuard()
    guard.add_protected_tool("pay")
    guard.record("pay", {}, result="ok", idempotency_key="tx-abc")
    assert guard.invalidate("pay", {}, idempotency_key="tx-abc") is True
    status, _ = guard.check("pay", {}, idempotency_key="tx-abc")
    assert status == DedupeResult.ALLOWED


def test_client_key_vs_auto_key_independent():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    args = {"to": "a@b.com"}
    guard.record("send_email", args, result="auto")
    guard.record("send_email", args, result="manual", idempotency_key="my-key")
    status1, cached1 = guard.check("send_email", args)
    status2, cached2 = guard.check("send_email", {}, idempotency_key="my-key")
    assert cached1 == "auto"
    assert cached2 == "manual"


# --- Pluggable backend ---

def test_memory_backend_default():
    guard = IdempotencyGuard()
    assert isinstance(guard.backend, MemoryBackend)


def test_custom_backend():
    class DictBackend(IdempotencyBackend):
        def __init__(self):
            self.store = {}
        def get(self, key):
            rec = self.store.get(key)
            if rec and time.time() - rec.timestamp > rec.ttl:
                del self.store[key]
                return None
            return rec
        def set(self, key, record):
            self.store[key] = record
        def delete(self, key):
            return self.store.pop(key, None) is not None
        def clear(self, tool_name=None):
            if tool_name is None:
                c = len(self.store)
                self.store.clear()
                return c
            keys = [k for k, v in self.store.items() if v.tool_name == tool_name]
            for k in keys:
                del self.store[k]
            return len(keys)
        def count(self):
            return len(self.store)

    backend = DictBackend()
    guard = IdempotencyGuard(backend=backend)
    guard.add_protected_tool("send_email")
    guard.record("send_email", {"to": "x@y.com"}, result="ok")
    assert len(backend.store) == 1
    status, cached = guard.check("send_email", {"to": "x@y.com"})
    assert status == DedupeResult.DUPLICATE
    assert cached == "ok"


def test_stats_shows_backend_name():
    guard = IdempotencyGuard()
    guard.add_protected_tool("tool_a")
    stats = guard.stats()
    assert stats["backend"] == "MemoryBackend"


def test_memory_backend_ttl_cleanup_on_count():
    backend = MemoryBackend()
    record = IdempotencyRecord(
        key="test:abc", tool_name="test", args_hash="abc",
        result="ok", timestamp=time.time() - 100, ttl=1.0,
    )
    backend.set("test:abc", record)
    assert backend.count() == 0


def test_memory_backend_get_expired_returns_none():
    backend = MemoryBackend()
    record = IdempotencyRecord(
        key="test:abc", tool_name="test", args_hash="abc",
        result="ok", timestamp=time.time() - 100, ttl=1.0,
    )
    backend.set("test:abc", record)
    assert backend.get("test:abc") is None
