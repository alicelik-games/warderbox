"""Rate limiting for agent tool calls.

Supports per-tool and global limits with sliding-window counters.

Usage:
    limiter = RateLimiter(
        global_rpm=60,
        tool_limits={"send_email": ToolLimit(rpm=5, rph=20)},
    )

    if limiter.would_exceed("send_email"):
        # block or queue

    limiter.record("send_email")
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from warderbox.state import StateBackend, StateBackendError


@dataclass
class ToolLimit:
    """Rate limit for a specific tool."""
    rpm: int | None = None
    rph: int | None = None


class RateLimiter:
    """Sliding-window rate limiter for agent actions."""

    def __init__(
        self,
        global_rpm: int | None = None,
        global_rph: int | None = None,
        tool_limits: dict[str, ToolLimit] | None = None,
        backend: StateBackend | None = None,
        namespace: str = "default",
    ) -> None:
        self._backend = backend
        self._namespace = namespace
        self._global_rpm = global_rpm
        self._global_rph = global_rph
        self._tool_limits = tool_limits or {}
        self._timestamps: list[float] = []
        self._tool_timestamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, stamps: list[float], window: float) -> list[float]:
        cutoff = time.time() - window
        return [t for t in stamps if t > cutoff]

    def _count_in_window(self, stamps: list[float], window: float) -> int:
        cutoff = time.time() - window
        return sum(1 for t in stamps if t > cutoff)

    _GLOBAL_KEY = "__global__"

    def _checks(self, tool_name: str) -> list[tuple[str, float, int, str]]:
        """Every (key, window, limit, message) applying to a tool."""
        checks: list[tuple[str, float, int, str]] = []
        if self._global_rpm is not None:
            checks.append((
                self._GLOBAL_KEY, 60, self._global_rpm,
                f"Global rate limit exceeded ({self._global_rpm}/min)",
            ))
        if self._global_rph is not None:
            checks.append((
                self._GLOBAL_KEY, 3600, self._global_rph,
                f"Global rate limit exceeded ({self._global_rph}/hour)",
            ))
        limit = self._tool_limits.get(tool_name)
        if limit is not None:
            key = f"tool:{tool_name}"
            if limit.rpm is not None:
                checks.append((
                    key, 60, limit.rpm,
                    f"Tool '{tool_name}' rate limit exceeded ({limit.rpm}/min)",
                ))
            if limit.rph is not None:
                checks.append((
                    key, 3600, limit.rph,
                    f"Tool '{tool_name}' rate limit exceeded ({limit.rph}/hour)",
                ))
        return checks

    def _hit_keys(self, tool_name: str) -> list[str]:
        return [self._GLOBAL_KEY, f"tool:{tool_name}"]

    def would_exceed(self, tool_name: str) -> tuple[bool, str | None]:
        """Check if calling this tool would exceed any rate limit.

        Returns (exceeded, reason) — reason is None if not exceeded.
        """
        if self._backend is not None:
            for key, window, limit, message in self._checks(tool_name):
                if self._backend.window_count(self._namespace, key, window) >= limit:
                    return True, message
            return False, None
        with self._lock:
            return self._would_exceed_locked(tool_name)

    def _would_exceed_locked(self, tool_name: str) -> tuple[bool, str | None]:
        if self._global_rpm is not None:
            if self._count_in_window(self._timestamps, 60) >= self._global_rpm:
                return True, f"Global rate limit exceeded ({self._global_rpm}/min)"

        if self._global_rph is not None:
            if self._count_in_window(self._timestamps, 3600) >= self._global_rph:
                return True, f"Global rate limit exceeded ({self._global_rph}/hour)"

        if tool_name in self._tool_limits:
            limit = self._tool_limits[tool_name]
            stamps = self._tool_timestamps.get(tool_name, [])

            if limit.rpm is not None:
                if self._count_in_window(stamps, 60) >= limit.rpm:
                    return True, f"Tool '{tool_name}' rate limit exceeded ({limit.rpm}/min)"

            if limit.rph is not None:
                if self._count_in_window(stamps, 3600) >= limit.rph:
                    return True, f"Tool '{tool_name}' rate limit exceeded ({limit.rph}/hour)"

        return False, None

    def reserve(self, tool_name: str) -> tuple[bool, str | None]:
        """Atomically check and record a call; (False, reason) if it would
        exceed a limit (nothing recorded). Closes the check-then-record race.

        With a backend the limit holds across workers, and this raises
        StateBackendError if the backend cannot be reached."""
        if self._backend is not None:
            checks = self._checks(tool_name)
            violated = self._backend.window_reserve(
                self._namespace,
                [(key, window, limit) for key, window, limit, _msg in checks],
                self._hit_keys(tool_name),
            )
            return (True, None) if violated is None else (False, checks[violated][3])
        with self._lock:
            exceeded, reason = self._would_exceed_locked(tool_name)
            if exceeded:
                return False, reason
            self._record_locked(tool_name)
            return True, None

    def release(self, tool_name: str) -> None:
        """Roll back the most recent recorded call for a tool (dry-run/dedupe)."""
        if self._backend is not None:
            self._backend.window_release(self._namespace, self._hit_keys(tool_name))
            return
        with self._lock:
            if self._timestamps:
                self._timestamps.pop()
            stamps = self._tool_timestamps.get(tool_name)
            if stamps:
                stamps.pop()

    def record(self, tool_name: str) -> None:
        """Record a tool call."""
        if self._backend is not None:
            self._backend.window_add(self._namespace, self._hit_keys(tool_name))
            return
        with self._lock:
            self._record_locked(tool_name)

    def _record_locked(self, tool_name: str) -> None:
        now = time.time()
        self._timestamps.append(now)
        if tool_name not in self._tool_timestamps:
            self._tool_timestamps[tool_name] = []
        self._tool_timestamps[tool_name].append(now)

        if len(self._timestamps) > 10000:
            self._timestamps = self._prune(self._timestamps, 3600)
        if len(self._tool_timestamps.get(tool_name, [])) > 5000:
            self._tool_timestamps[tool_name] = self._prune(
                self._tool_timestamps[tool_name], 3600
            )

    def stats(self) -> dict:
        """Current rate limit stats."""
        if self._backend is not None:
            backend, ns = self._backend, self._namespace
            try:
                by_tool = {
                    key[len("tool:"):]: {
                        "last_minute": backend.window_count(ns, key, 60),
                        "last_hour": backend.window_count(ns, key, 3600),
                    }
                    for key in backend.window_keys(ns) if key.startswith("tool:")
                }
                return {
                    "global_last_minute": backend.window_count(ns, self._GLOBAL_KEY, 60),
                    "global_last_hour": backend.window_count(ns, self._GLOBAL_KEY, 3600),
                    "by_tool": by_tool,
                }
            except StateBackendError:
                return {
                    "global_last_minute": 0,
                    "global_last_hour": 0,
                    "by_tool": {},
                    "degraded": True,
                }
        with self._lock:
            result: dict = {
                "global_last_minute": self._count_in_window(self._timestamps, 60),
                "global_last_hour": self._count_in_window(self._timestamps, 3600),
            }
            tool_stats = {}
            for name, stamps in self._tool_timestamps.items():
                tool_stats[name] = {
                    "last_minute": self._count_in_window(stamps, 60),
                    "last_hour": self._count_in_window(stamps, 3600),
                }
            result["by_tool"] = tool_stats
            return result
