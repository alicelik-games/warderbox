"""Idempotency guard for agent tool calls.

Prevents duplicate execution of the same logical operation,
so retries of send_email or payment calls don't double-fire.

Supports pluggable backends: in-memory (default) or any external
store (Redis, database) via the IdempotencyBackend protocol.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DedupeResult(Enum):
    ALLOWED = "allowed"
    DUPLICATE = "duplicate"


@dataclass
class IdempotencyRecord:
    """A record of a previously executed action."""

    key: str
    tool_name: str
    args_hash: str
    result: Any = None
    timestamp: float = field(default_factory=time.time)
    ttl: float = 3600.0
    in_flight: bool = False


class IdempotencyBackend(ABC):
    """Abstract backend for idempotency record storage."""

    @abstractmethod
    def get(self, key: str) -> IdempotencyRecord | None: ...

    @abstractmethod
    def set(self, key: str, record: IdempotencyRecord) -> None: ...

    def set_if_absent(self, key: str, record: IdempotencyRecord) -> bool:
        """Store the record only if the key is absent; return True if stored.

        Used to reserve an in-flight slot so concurrent or retried calls
        cannot both execute a protected tool. The default implementation is a
        non-atomic get-then-set fallback for third-party backends; built-in
        backends override it with a truly atomic version (lock / Redis SET NX).
        """
        if self.get(key) is not None:
            return False
        self.set(key, record)
        return True

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    @abstractmethod
    def clear(self, tool_name: str | None = None) -> int: ...

    @abstractmethod
    def count(self) -> int: ...


class MemoryBackend(IdempotencyBackend):
    """Thread-safe in-memory backend (default)."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return None
            if time.time() - record.timestamp > record.ttl:
                del self._records[key]
                return None
            return record

    def set(self, key: str, record: IdempotencyRecord) -> None:
        with self._lock:
            self._records[key] = record

    def set_if_absent(self, key: str, record: IdempotencyRecord) -> bool:
        with self._lock:
            existing = self._records.get(key)
            if existing is not None and time.time() - existing.timestamp <= existing.ttl:
                return False
            self._records[key] = record
            return True

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._records.pop(key, None) is not None

    def clear(self, tool_name: str | None = None) -> int:
        with self._lock:
            if tool_name is None:
                count = len(self._records)
                self._records.clear()
                return count
            keys = [k for k, v in self._records.items() if v.tool_name == tool_name]
            for k in keys:
                del self._records[k]
            return len(keys)

    def count(self) -> int:
        with self._lock:
            now = time.time()
            expired = [
                k for k, v in self._records.items()
                if now - v.timestamp > v.ttl
            ]
            for k in expired:
                del self._records[k]
            return len(self._records)


class RedisBackend(IdempotencyBackend):
    """Distributed idempotency backend backed by Redis.

    Enables cross-process / multi-worker deduplication. Requires the
    optional ``redis`` package (``pip install redis``). Records expire
    via Redis' native TTL.

    Usage:
        guard = IdempotencyGuard(backend=RedisBackend(url="redis://localhost:6379/0"))
        # or pass an existing client:
        guard = IdempotencyGuard(backend=RedisBackend(client=my_redis))
    """

    def __init__(
        self,
        client: Any = None,
        url: str = "redis://localhost:6379/0",
        namespace: str = "warderbox:idem",
    ) -> None:
        if client is None:
            try:
                import redis
            except ImportError as e:  # pragma: no cover - optional dep
                raise ImportError(
                    "RedisBackend requires the 'redis' package. "
                    "Install it with: pip install redis"
                ) from e
            client = redis.Redis.from_url(url)
        self._r = client
        self._ns = namespace

    def _k(self, key: str) -> str:
        return f"{self._ns}:rec:{key}"

    def _tool_index(self, tool_name: str) -> str:
        return f"{self._ns}:tool:{tool_name}"

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else value

    def get(self, key: str) -> IdempotencyRecord | None:
        raw = self._r.get(self._k(key))
        if raw is None:
            return None
        data = json.loads(self._decode(raw))
        return IdempotencyRecord(**data)

    def set(self, key: str, record: IdempotencyRecord) -> None:
        data = {
            "key": record.key,
            "tool_name": record.tool_name,
            "args_hash": record.args_hash,
            "result": record.result,
            "timestamp": record.timestamp,
            "ttl": record.ttl,
            "in_flight": record.in_flight,
        }
        ttl = max(1, int(record.ttl))
        self._r.set(self._k(key), json.dumps(data, default=str), ex=ttl)
        self._r.sadd(self._tool_index(record.tool_name), key)
        self._r.expire(self._tool_index(record.tool_name), ttl)

    def set_if_absent(self, key: str, record: IdempotencyRecord) -> bool:
        data = {
            "key": record.key,
            "tool_name": record.tool_name,
            "args_hash": record.args_hash,
            "result": record.result,
            "timestamp": record.timestamp,
            "ttl": record.ttl,
            "in_flight": record.in_flight,
        }
        ttl = max(1, int(record.ttl))
        # Redis SET NX gives true cross-process atomicity.
        stored = self._r.set(
            self._k(key), json.dumps(data, default=str), nx=True, ex=ttl
        )
        if stored:
            self._r.sadd(self._tool_index(record.tool_name), key)
            self._r.expire(self._tool_index(record.tool_name), ttl)
        return bool(stored)

    def delete(self, key: str) -> bool:
        return bool(self._r.delete(self._k(key)))

    def clear(self, tool_name: str | None = None) -> int:
        if tool_name is None:
            keys = list(self._r.scan_iter(f"{self._ns}:rec:*"))
            if keys:
                self._r.delete(*keys)
            return len(keys)
        members = self._r.smembers(self._tool_index(tool_name))
        count = 0
        for m in members:
            if self._r.delete(self._k(self._decode(m))):
                count += 1
        self._r.delete(self._tool_index(tool_name))
        return count

    def count(self) -> int:
        return sum(1 for _ in self._r.scan_iter(f"{self._ns}:rec:*"))


class IdempotencyGuard:
    """Detects and blocks duplicate tool calls.

    An idempotency key is computed from the tool name and its arguments,
    or supplied by the caller for distributed coordination.
    If the same key appears within the TTL window, the call is blocked
    and the cached result is returned instead.

    Usage:
        guard = IdempotencyGuard(default_ttl=3600)
        guard.add_protected_tool("send_email")

        # Auto-generated key from args:
        status, cached = guard.check("send_email", {"to": "a@b.com", "body": "hi"})

        # Client-supplied key for distributed dedup:
        status, cached = guard.check("send_email", {"to": "a@b.com"}, idempotency_key="order-123-email")

        # Pluggable backend:
        guard = IdempotencyGuard(backend=RedisBackend(redis_client))
    """

    def __init__(
        self,
        default_ttl: float = 3600.0,
        protected_tools: set[str] | None = None,
        backend: IdempotencyBackend | None = None,
    ) -> None:
        self._default_ttl = default_ttl
        self._protected_tools: set[str] = protected_tools or set()
        self._backend = backend or MemoryBackend()
        self._duplicates_blocked = 0
        self._lock = threading.Lock()

    @property
    def backend(self) -> IdempotencyBackend:
        return self._backend

    def add_protected_tool(self, tool_name: str, ttl: float | None = None) -> None:
        self._protected_tools.add(tool_name)

    def remove_protected_tool(self, tool_name: str) -> None:
        self._protected_tools.discard(tool_name)

    def is_protected(self, tool_name: str) -> bool:
        return tool_name in self._protected_tools

    @staticmethod
    def _hash_args(args: dict) -> str:
        serialized = json.dumps(args, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    @staticmethod
    def _make_key(tool_name: str, args_hash: str) -> str:
        return f"{tool_name}:{args_hash}"

    def check(
        self,
        tool_name: str,
        args: dict,
        idempotency_key: str | None = None,
    ) -> tuple[DedupeResult, Any | None]:
        """Check if this call is a duplicate.

        Returns (ALLOWED, None) for new calls, or
        (DUPLICATE, cached_result) for repeated ones.

        If idempotency_key is provided, it is used directly instead of
        hashing the args. This enables distributed deduplication where
        multiple processes share the same backend.
        """
        if tool_name not in self._protected_tools:
            return DedupeResult.ALLOWED, None

        if idempotency_key is not None:
            key = f"{tool_name}:{idempotency_key}"
        else:
            args_hash = self._hash_args(args)
            key = self._make_key(tool_name, args_hash)

        record = self._backend.get(key)
        if record is not None:
            with self._lock:
                self._duplicates_blocked += 1
            return DedupeResult.DUPLICATE, record.result
        return DedupeResult.ALLOWED, None

    def record(
        self,
        tool_name: str,
        args: dict,
        result: Any = None,
        ttl: float | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Record a completed execution for deduplication."""
        if idempotency_key is not None:
            key = f"{tool_name}:{idempotency_key}"
            args_hash = idempotency_key
        else:
            args_hash = self._hash_args(args)
            key = self._make_key(tool_name, args_hash)

        entry = IdempotencyRecord(
            key=key,
            tool_name=tool_name,
            args_hash=args_hash,
            result=result,
            ttl=ttl or self._default_ttl,
        )
        self._backend.set(key, entry)
        return key

    def _key_for(self, tool_name: str, args: dict, idempotency_key: str | None) -> str:
        if idempotency_key is not None:
            return f"{tool_name}:{idempotency_key}"
        return self._make_key(tool_name, self._hash_args(args))

    def begin(
        self,
        tool_name: str,
        args: dict,
        idempotency_key: str | None = None,
    ) -> tuple[DedupeResult, Any | None]:
        """Atomically reserve an in-flight slot for a protected tool.

        Returns (ALLOWED, None) when the caller won the reservation and may
        execute, or (DUPLICATE, cached) when a completed or in-flight record
        already exists. This closes the check-then-act race so concurrent
        calls and caller-level retries cannot double-execute a side effect.
        """
        if tool_name not in self._protected_tools:
            return DedupeResult.ALLOWED, None

        key = self._key_for(tool_name, args, idempotency_key)
        marker = IdempotencyRecord(
            key=key,
            tool_name=tool_name,
            args_hash=key,
            result=None,
            ttl=self._default_ttl,
            in_flight=True,
        )
        if self._backend.set_if_absent(key, marker):
            return DedupeResult.ALLOWED, None

        existing = self._backend.get(key)
        with self._lock:
            self._duplicates_blocked += 1
        return DedupeResult.DUPLICATE, existing.result if existing else None

    def complete(
        self,
        tool_name: str,
        args: dict,
        result: Any = None,
        ttl: float | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Finalize a reserved slot with the real result."""
        return self.record(
            tool_name, args, result=result, ttl=ttl, idempotency_key=idempotency_key
        )

    def fail(
        self,
        tool_name: str,
        args: dict,
        keep: bool = True,
        idempotency_key: str | None = None,
    ) -> None:
        """Resolve a reserved slot after a failed execution.

        By default the in-flight marker is *kept*, so a caller retry with the
        same arguments is blocked — the side effect may already have landed
        (e.g. a payment that committed before the response timed out). Pass
        keep=False only for tools known to have no side effect before failure.
        """
        if not keep:
            key = self._key_for(tool_name, args, idempotency_key)
            self._backend.delete(key)

    def invalidate(
        self,
        tool_name: str,
        args: dict,
        idempotency_key: str | None = None,
    ) -> bool:
        """Remove a specific idempotency record (allow re-execution)."""
        if idempotency_key is not None:
            key = f"{tool_name}:{idempotency_key}"
        else:
            args_hash = self._hash_args(args)
            key = self._make_key(tool_name, args_hash)
        return self._backend.delete(key)

    def clear(self, tool_name: str | None = None) -> int:
        """Clear all records, or only for a specific tool."""
        return self._backend.clear(tool_name)

    def stats(self) -> dict:
        with self._lock:
            return {
                "protected_tools": sorted(self._protected_tools),
                "active_records": self._backend.count(),
                "duplicates_blocked": self._duplicates_blocked,
                "default_ttl": self._default_ttl,
                "backend": type(self._backend).__name__,
            }
