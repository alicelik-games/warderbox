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
    ) -> None:
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

    def would_exceed(self, tool_name: str) -> tuple[bool, str | None]:
        """Check if calling this tool would exceed any rate limit.

        Returns (exceeded, reason) — reason is None if not exceeded.
        """
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
        exceed a limit (nothing recorded). Closes the check-then-record race."""
        with self._lock:
            exceeded, reason = self._would_exceed_locked(tool_name)
            if exceeded:
                return False, reason
            self._record_locked(tool_name)
            return True, None

    def release(self, tool_name: str) -> None:
        """Roll back the most recent recorded call for a tool (dry-run/dedupe)."""
        with self._lock:
            if self._timestamps:
                self._timestamps.pop()
            stamps = self._tool_timestamps.get(tool_name)
            if stamps:
                stamps.pop()

    def record(self, tool_name: str) -> None:
        """Record a tool call."""
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
