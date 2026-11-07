"""Shared state backends for budget, rate limit and quota enforcement.

Without a backend, limits are enforced per process: four workers with a $5
budget can spend $20 between them. A backend moves the counters somewhere all
workers can reach and makes each check-and-reserve atomic.

``SQLiteStateBackend``
    Workers on one machine. No dependencies. Not for network filesystems.
``RedisStateBackend``
    Workers across machines. Requires ``pip install warderbox[redis]``.

Reservations carry a time-to-live, so budget held by a crashed worker returns
on its own. When a backend is unreachable, reservations fail and the call is
blocked (fail closed).

Usage:
    backend = SQLiteStateBackend("warderbox-state.db")
    shield = WarderBox(budget=5.0, state_backend=backend, namespace="prod")
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# (key, window seconds, limit) and (key, limit, period seconds).
WindowCheck = tuple[str, float, int]
CounterCheck = tuple[str, int, float]

_WINDOW_RETENTION = 7200.0
_EPSILON = 1e-10


class StateBackendError(Exception):
    """The shared state store could not be reached or failed."""


class StateBackend:
    """Atomic primitives the budget, rate and quota trackers are built on."""

    # Budget ---------------------------------------------------------------
    def budget_reserve(
        self, ns: str, reservation_id: str, amount: float,
        limit: float | None, ttl: float,
    ) -> bool:
        """Hold *amount* unless spent + held + amount would exceed *limit*."""
        raise NotImplementedError

    def budget_commit(self, ns: str, reservation_id: str, cost: float) -> None:
        """Drop the reservation (if it has not expired) and add *cost*."""
        raise NotImplementedError

    def budget_release(self, ns: str, reservation_id: str) -> bool:
        raise NotImplementedError

    def budget_adjust(self, ns: str, delta: float) -> None:
        raise NotImplementedError

    def budget_totals(self, ns: str) -> tuple[float, float, int]:
        """Return (spent, reserved, active reservations)."""
        raise NotImplementedError

    def budget_reset(self, ns: str, keep_reservations: bool = True) -> None:
        raise NotImplementedError

    # Sliding windows ------------------------------------------------------
    def window_reserve(
        self, ns: str, checks: list[WindowCheck], keys: list[str]
    ) -> int | None:
        """Add a hit to *keys* if no check is at its limit.

        Returns the index of the first violated check, or None on success.
        """
        raise NotImplementedError

    def window_add(self, ns: str, keys: list[str]) -> None:
        raise NotImplementedError

    def window_release(self, ns: str, keys: list[str]) -> None:
        """Remove the most recent hit from each key."""
        raise NotImplementedError

    def window_count(self, ns: str, key: str, window: float) -> int:
        raise NotImplementedError

    def window_keys(self, ns: str) -> list[str]:
        raise NotImplementedError

    # Fixed-period counters ------------------------------------------------
    def counter_reserve(
        self, ns: str, checks: list[CounterCheck]
    ) -> tuple[int, int] | None:
        """Increment every counter if none is at its limit.

        Returns (index of first violated check, its count), or None on success.
        """
        raise NotImplementedError

    def counter_add(self, ns: str, checks: list[CounterCheck]) -> None:
        raise NotImplementedError

    def counter_release(self, ns: str, keys: list[str]) -> None:
        raise NotImplementedError

    def counter_get(self, ns: str, key: str, period: float) -> tuple[int, float]:
        """Return (count, window start), resetting an elapsed period."""
        raise NotImplementedError

    def close(self) -> None:
        pass


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget (ns TEXT PRIMARY KEY, spent REAL NOT NULL);
CREATE TABLE IF NOT EXISTS budget_reservations (
    ns TEXT NOT NULL, id TEXT NOT NULL, amount REAL NOT NULL,
    expires REAL NOT NULL, PRIMARY KEY (ns, id)
);
CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ns TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_windows ON windows(ns, key, ts);
CREATE TABLE IF NOT EXISTS counters (
    ns TEXT NOT NULL, key TEXT NOT NULL, count INTEGER NOT NULL,
    window_start REAL NOT NULL, PRIMARY KEY (ns, key)
);
"""


class SQLiteStateBackend(StateBackend):
    """Shared state in one SQLite file, safe across threads and processes."""

    def __init__(self, path: str | Path, busy_timeout: float = 30.0) -> None:
        self._path = str(path)
        self._busy_timeout = busy_timeout
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._pid = os.getpid()
        with self._tx():
            pass

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                self._path, timeout=self._busy_timeout,
                isolation_level=None, check_same_thread=False,
            )
            conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout * 1000)}")
            for attempt in range(50):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(_SQLITE_SCHEMA)
                    break
                except sqlite3.OperationalError:
                    # Another process is creating the same database.
                    if attempt == 49:
                        raise
                    time.sleep(0.1)
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if os.getpid() != self._pid:
            # A connection must not cross fork(); the child opens its own.
            self._pid = os.getpid()
            self._lock = threading.Lock()
            self._conn = None
        with self._lock:
            try:
                conn = self._connection()
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as e:
                raise StateBackendError(f"SQLite state backend: {e}") from e
            try:
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as e:
                self._rollback(conn)
                raise StateBackendError(f"SQLite state backend: {e}") from e
            except BaseException:
                self._rollback(conn)
                raise

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # Budget ---------------------------------------------------------------
    @staticmethod
    def _purge(conn: sqlite3.Connection, ns: str) -> None:
        conn.execute(
            "DELETE FROM budget_reservations WHERE ns = ? AND expires <= ?",
            (ns, time.time()),
        )

    @staticmethod
    def _totals(conn: sqlite3.Connection, ns: str) -> tuple[float, float, int]:
        row = conn.execute(
            "SELECT spent FROM budget WHERE ns = ?", (ns,)
        ).fetchone()
        held = conn.execute(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) "
            "FROM budget_reservations WHERE ns = ?",
            (ns,),
        ).fetchone()
        return (row[0] if row else 0.0), held[0], held[1]

    @staticmethod
    def _add_spent(conn: sqlite3.Connection, ns: str, delta: float) -> None:
        conn.execute(
            "INSERT INTO budget (ns, spent) VALUES (?, ?) "
            "ON CONFLICT(ns) DO UPDATE SET spent = spent + excluded.spent",
            (ns, delta),
        )

    def budget_reserve(self, ns, reservation_id, amount, limit, ttl) -> bool:
        with self._tx() as conn:
            self._purge(conn, ns)
            if limit is not None:
                spent, held, _ = self._totals(conn, ns)
                if spent + held + amount > limit + _EPSILON:
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO budget_reservations "
                "(ns, id, amount, expires) VALUES (?, ?, ?, ?)",
                (ns, reservation_id, amount, time.time() + ttl),
            )
            return True

    def budget_commit(self, ns, reservation_id, cost) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM budget_reservations WHERE ns = ? AND id = ?",
                (ns, reservation_id),
            )
            self._add_spent(conn, ns, cost)

    def budget_release(self, ns, reservation_id) -> bool:
        with self._tx() as conn:
            cursor = conn.execute(
                "DELETE FROM budget_reservations WHERE ns = ? AND id = ?",
                (ns, reservation_id),
            )
            return cursor.rowcount > 0

    def budget_adjust(self, ns, delta) -> None:
        with self._tx() as conn:
            self._add_spent(conn, ns, delta)

    def budget_totals(self, ns):
        with self._tx() as conn:
            self._purge(conn, ns)
            return self._totals(conn, ns)

    def budget_reset(self, ns, keep_reservations=True) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM budget WHERE ns = ?", (ns,))
            if not keep_reservations:
                conn.execute(
                    "DELETE FROM budget_reservations WHERE ns = ?", (ns,)
                )

    # Sliding windows ------------------------------------------------------
    @staticmethod
    def _insert_hits(conn, ns: str, keys: list[str], now: float) -> None:
        for key in keys:
            conn.execute(
                "INSERT INTO windows (ns, key, ts) VALUES (?, ?, ?)",
                (ns, key, now),
            )
            conn.execute(
                "DELETE FROM windows WHERE ns = ? AND key = ? AND ts <= ?",
                (ns, key, now - _WINDOW_RETENTION),
            )

    def window_reserve(self, ns, checks, keys):
        now = time.time()
        with self._tx() as conn:
            for index, (key, window, limit) in enumerate(checks):
                count = conn.execute(
                    "SELECT COUNT(*) FROM windows "
                    "WHERE ns = ? AND key = ? AND ts > ?",
                    (ns, key, now - window),
                ).fetchone()[0]
                if count >= limit:
                    return index
            self._insert_hits(conn, ns, keys, now)
            return None

    def window_add(self, ns, keys) -> None:
        with self._tx() as conn:
            self._insert_hits(conn, ns, keys, time.time())

    def window_release(self, ns, keys) -> None:
        with self._tx() as conn:
            for key in keys:
                conn.execute(
                    "DELETE FROM windows WHERE id = ("
                    "SELECT id FROM windows WHERE ns = ? AND key = ? "
                    "ORDER BY id DESC LIMIT 1)",
                    (ns, key),
                )

    def window_count(self, ns, key, window) -> int:
        with self._tx() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM windows "
                "WHERE ns = ? AND key = ? AND ts > ?",
                (ns, key, time.time() - window),
            ).fetchone()[0]

    def window_keys(self, ns) -> list[str]:
        with self._tx() as conn:
            return [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT key FROM windows WHERE ns = ?", (ns,)
                )
            ]

    # Fixed-period counters ------------------------------------------------
    @staticmethod
    def _current(conn, ns: str, key: str, period: float, now: float) -> tuple[int, float]:
        row = conn.execute(
            "SELECT count, window_start FROM counters WHERE ns = ? AND key = ?",
            (ns, key),
        ).fetchone()
        if row is None or now - row[1] >= period:
            conn.execute(
                "INSERT OR REPLACE INTO counters (ns, key, count, window_start) "
                "VALUES (?, ?, 0, ?)",
                (ns, key, now),
            )
            return 0, now
        return row[0], row[1]

    def counter_reserve(self, ns, checks):
        now = time.time()
        with self._tx() as conn:
            for index, (key, limit, period) in enumerate(checks):
                count, _start = self._current(conn, ns, key, period, now)
                if count >= limit:
                    return index, count
            for key, _limit, _period in checks:
                conn.execute(
                    "UPDATE counters SET count = count + 1 WHERE ns = ? AND key = ?",
                    (ns, key),
                )
            return None

    def counter_add(self, ns, checks) -> None:
        now = time.time()
        with self._tx() as conn:
            for key, _limit, period in checks:
                self._current(conn, ns, key, period, now)
                conn.execute(
                    "UPDATE counters SET count = count + 1 WHERE ns = ? AND key = ?",
                    (ns, key),
                )

    def counter_release(self, ns, keys) -> None:
        with self._tx() as conn:
            for key in keys:
                conn.execute(
                    "UPDATE counters SET count = count - 1 "
                    "WHERE ns = ? AND key = ? AND count > 0",
                    (ns, key),
                )

    def counter_get(self, ns, key, period):
        with self._tx() as conn:
            return self._current(conn, ns, key, period, time.time())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


_LUA_BUDGET_RESERVE = """
for _, id in ipairs(redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[5])) do
  redis.call('HDEL', KEYS[3], id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[5])
if ARGV[3] ~= '' then
  local spent = tonumber(redis.call('HGET', KEYS[1], 'spent') or '0')
  local held = 0
  for _, v in ipairs(redis.call('HVALS', KEYS[3])) do
    held = held + tonumber(v)
  end
  if spent + held + tonumber(ARGV[2]) > tonumber(ARGV[3]) + 1e-10 then
    return 0
  end
end
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
return 1
"""

_LUA_BUDGET_COMMIT = """
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('HINCRBYFLOAT', KEYS[1], 'spent', ARGV[2])
return 1
"""

_LUA_BUDGET_RELEASE = """
redis.call('ZREM', KEYS[2], ARGV[1])
return redis.call('HDEL', KEYS[3], ARGV[1])
"""

_LUA_BUDGET_TOTALS = """
for _, id in ipairs(redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])) do
  redis.call('HDEL', KEYS[3], id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
local held = 0
local values = redis.call('HVALS', KEYS[3])
for _, v in ipairs(values) do held = held + tonumber(v) end
return {redis.call('HGET', KEYS[1], 'spent') or '0',
        string.format('%.17g', held), #values}
"""

# KEYS: one zset per check, then one zset per key to add, then the key set.
# ARGV: now, check count, add count, retention, member, then window+limit pairs,
# then the logical names of the added keys.
_LUA_WINDOW_RESERVE = """
local now = tonumber(ARGV[1])
local checks = tonumber(ARGV[2])
local adds = tonumber(ARGV[3])
for i = 1, checks do
  local window = tonumber(ARGV[4 + i * 2])
  local limit = tonumber(ARGV[5 + i * 2])
  if redis.call('ZCOUNT', KEYS[i], '(' .. (now - window), '+inf') >= limit then
    return i - 1
  end
end
for i = 1, adds do
  local key = KEYS[checks + i]
  redis.call('ZADD', key, now, ARGV[5] .. ':' .. i)
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - tonumber(ARGV[4]))
  redis.call('EXPIRE', key, math.ceil(tonumber(ARGV[4])))
  redis.call('SADD', KEYS[checks + adds + 1], ARGV[5 + checks * 2 + i])
end
return -1
"""

_LUA_WINDOW_RELEASE = """
for i = 1, #KEYS do
  redis.call('ZPOPMAX', KEYS[i])
end
return 1
"""

_LUA_COUNTER_RESERVE = """
local now = tonumber(ARGV[1])
local force = ARGV[2] == '1'
local counts = {}
for i = 1, #KEYS do
  local limit = tonumber(ARGV[1 + i * 2])
  local period = tonumber(ARGV[2 + i * 2])
  local start = tonumber(redis.call('HGET', KEYS[i], 'start') or '')
  local count = 0
  if start ~= nil and now - start < period then
    count = tonumber(redis.call('HGET', KEYS[i], 'count') or '0')
  else
    start = nil
  end
  if start == nil then
    redis.call('HSET', KEYS[i], 'count', 0, 'start', ARGV[1])
    redis.call('EXPIRE', KEYS[i], math.ceil(period * 2))
  end
  if not force and count >= limit then
    return {i - 1, count}
  end
end
for i = 1, #KEYS do
  redis.call('HINCRBY', KEYS[i], 'count', 1)
end
return {-1, 0}
"""

_LUA_COUNTER_RELEASE = """
for i = 1, #KEYS do
  if tonumber(redis.call('HGET', KEYS[i], 'count') or '0') > 0 then
    redis.call('HINCRBY', KEYS[i], 'count', -1)
  end
end
return 1
"""

_LUA_COUNTER_GET = """
local now = tonumber(ARGV[1])
local start = tonumber(redis.call('HGET', KEYS[1], 'start') or '')
if start == nil or now - start >= tonumber(ARGV[2]) then
  redis.call('HSET', KEYS[1], 'count', 0, 'start', ARGV[1])
  redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) * 2))
  return {0, ARGV[1]}
end
return {tonumber(redis.call('HGET', KEYS[1], 'count') or '0'), redis.call('HGET', KEYS[1], 'start')}
"""


class RedisStateBackend(StateBackend):
    """Shared state in Redis. Every operation is one atomic Lua script.

    All keys for a namespace share a hash tag, so this also works on Redis
    Cluster. Window timestamps come from each worker's clock, so keep clocks
    synchronized (NTP).
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        client: Any = None,
        prefix: str = "warderbox",
    ) -> None:
        try:
            import redis
        except ImportError as e:
            if client is None:
                raise ImportError(
                    "RedisStateBackend requires: pip install warderbox[redis]"
                ) from e
            self._errors: tuple[type[BaseException], ...] = (ConnectionError, OSError)
        else:
            self._errors = (redis.RedisError, OSError)
            if client is None:
                client = redis.Redis.from_url(url)
        self._client = client
        self._prefix = prefix
        self._scripts = {
            name: client.register_script(body)
            for name, body in {
                "budget_reserve": _LUA_BUDGET_RESERVE,
                "budget_commit": _LUA_BUDGET_COMMIT,
                "budget_release": _LUA_BUDGET_RELEASE,
                "budget_totals": _LUA_BUDGET_TOTALS,
                "window_reserve": _LUA_WINDOW_RESERVE,
                "window_release": _LUA_WINDOW_RELEASE,
                "counter_reserve": _LUA_COUNTER_RESERVE,
                "counter_release": _LUA_COUNTER_RELEASE,
                "counter_get": _LUA_COUNTER_GET,
            }.items()
        }

    def _key(self, ns: str, *parts: str) -> str:
        return ":".join((self._prefix, "{" + ns + "}", *parts))

    def _budget_keys(self, ns: str) -> list[str]:
        return [
            self._key(ns, "budget"),
            self._key(ns, "reservations"),
            self._key(ns, "amounts"),
        ]

    def _run(self, script: str, keys: list[str], args: list[Any]) -> Any:
        try:
            return self._scripts[script](keys=keys, args=args)
        except self._errors as e:
            raise StateBackendError(f"Redis state backend: {e}") from e

    def _call(self, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except self._errors as e:
            raise StateBackendError(f"Redis state backend: {e}") from e

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    # Budget ---------------------------------------------------------------
    def budget_reserve(self, ns, reservation_id, amount, limit, ttl) -> bool:
        now = time.time()
        return bool(self._run("budget_reserve", self._budget_keys(ns), [
            reservation_id, repr(float(amount)),
            "" if limit is None else repr(float(limit)),
            repr(now + ttl), repr(now),
        ]))

    def budget_commit(self, ns, reservation_id, cost) -> None:
        self._run("budget_commit", self._budget_keys(ns), [
            reservation_id, repr(float(cost)),
        ])

    def budget_release(self, ns, reservation_id) -> bool:
        return bool(self._run(
            "budget_release", self._budget_keys(ns), [reservation_id]
        ))

    def budget_adjust(self, ns, delta) -> None:
        self._call(
            self._client.hincrbyfloat, self._key(ns, "budget"), "spent", float(delta)
        )

    def budget_totals(self, ns):
        spent, held, count = self._run(
            "budget_totals", self._budget_keys(ns), [repr(time.time())]
        )
        return float(self._text(spent)), float(self._text(held)), int(count)

    def budget_reset(self, ns, keep_reservations=True) -> None:
        keys = self._budget_keys(ns)
        self._call(self._client.delete, *(keys[:1] if keep_reservations else keys))

    # Sliding windows ------------------------------------------------------
    def _window_key(self, ns: str, key: str) -> str:
        return self._key(ns, "window", key)

    def window_reserve(self, ns, checks, keys):
        redis_keys = [self._window_key(ns, c[0]) for c in checks]
        redis_keys += [self._window_key(ns, k) for k in keys]
        redis_keys.append(self._key(ns, "windows"))
        args: list[Any] = [
            repr(time.time()), len(checks), len(keys),
            repr(_WINDOW_RETENTION), uuid.uuid4().hex,
        ]
        for _key, window, limit in checks:
            args += [repr(float(window)), limit]
        args += list(keys)
        index = int(self._run("window_reserve", redis_keys, args))
        return None if index < 0 else index

    def window_add(self, ns, keys) -> None:
        self.window_reserve(ns, [], keys)

    def window_release(self, ns, keys) -> None:
        self._run(
            "window_release", [self._window_key(ns, k) for k in keys], []
        )

    def window_count(self, ns, key, window) -> int:
        return int(self._call(
            self._client.zcount, self._window_key(ns, key),
            f"({time.time() - window!r}", "+inf",
        ))

    def window_keys(self, ns) -> list[str]:
        members = self._call(self._client.smembers, self._key(ns, "windows"))
        return [self._text(m) for m in members]

    # Fixed-period counters ------------------------------------------------
    def _counter_key(self, ns: str, key: str) -> str:
        return self._key(ns, "counter", key)

    def _counter_run(self, ns: str, checks: list[CounterCheck], force: bool) -> list:
        args: list[Any] = [repr(time.time()), "1" if force else "0"]
        for _key, limit, period in checks:
            args += [limit, repr(float(period))]
        return self._run(
            "counter_reserve", [self._counter_key(ns, c[0]) for c in checks], args
        )

    def counter_reserve(self, ns, checks):
        if not checks:
            return None
        index, count = self._counter_run(ns, checks, force=False)
        return None if int(index) < 0 else (int(index), int(count))

    def counter_add(self, ns, checks) -> None:
        if checks:
            self._counter_run(ns, checks, force=True)

    def counter_release(self, ns, keys) -> None:
        if keys:
            self._run(
                "counter_release", [self._counter_key(ns, k) for k in keys], []
            )

    def counter_get(self, ns, key, period):
        count, start = self._run(
            "counter_get", [self._counter_key(ns, key)],
            [repr(time.time()), repr(float(period))],
        )
        return int(count), float(self._text(start))

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


def backend_from_config(config: dict[str, Any]) -> StateBackend:
    """Build a backend from a policy's ``state`` section."""
    kind = config.get("backend")
    if kind == "sqlite":
        if not config.get("path"):
            raise ValueError("state backend 'sqlite' requires 'path'")
        return SQLiteStateBackend(config["path"])
    if kind == "redis":
        return RedisStateBackend(
            url=config.get("url", "redis://localhost:6379/0"),
            prefix=config.get("prefix", "warderbox"),
        )
    raise ValueError(f"Unknown state backend {kind!r} (use 'sqlite' or 'redis')")
