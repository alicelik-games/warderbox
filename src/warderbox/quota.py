"""Execution quotas for agent tool calls.

Enforces daily, weekly, and monthly call limits per tool or agent,
complementing the sliding-window rate limiter with longer-term caps.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class QuotaPeriod(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


PERIOD_SECONDS = {
    QuotaPeriod.DAILY: 86400,
    QuotaPeriod.WEEKLY: 604800,
    QuotaPeriod.MONTHLY: 2592000,
}


@dataclass
class QuotaLimit:
    """A quota limit for a specific scope."""

    period: QuotaPeriod
    max_calls: int
    scope: str = "global"


@dataclass
class QuotaUsage:
    """Current usage for a quota."""

    limit: QuotaLimit
    current_calls: int = 0
    window_start: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return max(0, self.limit.max_calls - self.current_calls)

    @property
    def exceeded(self) -> bool:
        return self.current_calls >= self.limit.max_calls

    @property
    def utilization(self) -> float:
        if self.limit.max_calls == 0:
            return 1.0
        return self.current_calls / self.limit.max_calls


class QuotaManager:
    """Manages daily/weekly/monthly execution quotas.

    Usage:
        qm = QuotaManager()
        qm.set_quota("send_email", QuotaPeriod.DAILY, 100)
        qm.set_quota("api_call", QuotaPeriod.MONTHLY, 10000)

        exceeded, reason = qm.would_exceed("send_email")
        if exceeded:
            # block

        qm.record("send_email")
    """

    def __init__(self) -> None:
        self._quotas: dict[str, list[QuotaUsage]] = {}
        self._lock = threading.Lock()

    def set_quota(
        self,
        scope: str,
        period: QuotaPeriod,
        max_calls: int,
    ) -> None:
        """Set a quota for a scope (tool name, agent name, or 'global')."""
        limit = QuotaLimit(period=period, max_calls=max_calls, scope=scope)
        usage = QuotaUsage(limit=limit)
        with self._lock:
            if scope not in self._quotas:
                self._quotas[scope] = []
            existing = [u for u in self._quotas[scope] if u.limit.period != period]
            existing.append(usage)
            self._quotas[scope] = existing

    def remove_quota(self, scope: str, period: QuotaPeriod | None = None) -> bool:
        """Remove quota(s) for a scope."""
        with self._lock:
            if scope not in self._quotas:
                return False
            if period is None:
                del self._quotas[scope]
                return True
            before = len(self._quotas[scope])
            self._quotas[scope] = [
                u for u in self._quotas[scope] if u.limit.period != period
            ]
            if not self._quotas[scope]:
                del self._quotas[scope]
            return len(self._quotas.get(scope, [])) < before

    def would_exceed(self, tool_name: str) -> tuple[bool, str | None]:
        """Check if executing this tool would exceed any quota."""
        with self._lock:
            now = time.time()
            for scope in (tool_name, "global"):
                usages = self._quotas.get(scope, [])
                for usage in usages:
                    self._maybe_reset(usage, now)
                    if usage.current_calls >= usage.limit.max_calls:
                        period_name = usage.limit.period.value
                        return True, (
                            f"Quota exceeded for '{scope}': "
                            f"{usage.current_calls}/{usage.limit.max_calls} {period_name}"
                        )
            return False, None

    def record(self, tool_name: str) -> None:
        """Record a tool execution against all applicable quotas."""
        with self._lock:
            now = time.time()
            for scope in (tool_name, "global"):
                usages = self._quotas.get(scope, [])
                for usage in usages:
                    self._maybe_reset(usage, now)
                    usage.current_calls += 1

    def get_usage(self, scope: str) -> list[dict]:
        """Get current usage for a scope."""
        with self._lock:
            now = time.time()
            usages = self._quotas.get(scope, [])
            result = []
            for usage in usages:
                self._maybe_reset(usage, now)
                result.append({
                    "period": usage.limit.period.value,
                    "max_calls": usage.limit.max_calls,
                    "current_calls": usage.current_calls,
                    "remaining": usage.remaining,
                    "utilization": round(usage.utilization, 4),
                })
            return result

    def _maybe_reset(self, usage: QuotaUsage, now: float) -> None:
        window = PERIOD_SECONDS[usage.limit.period]
        if now - usage.window_start >= window:
            usage.current_calls = 0
            usage.window_start = now

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            scopes = {}
            total_exceeded = 0
            for scope, usages in self._quotas.items():
                scope_info = []
                for usage in usages:
                    self._maybe_reset(usage, now)
                    if usage.exceeded:
                        total_exceeded += 1
                    scope_info.append({
                        "period": usage.limit.period.value,
                        "max_calls": usage.limit.max_calls,
                        "current_calls": usage.current_calls,
                        "remaining": usage.remaining,
                        "exceeded": usage.exceeded,
                    })
                scopes[scope] = scope_info
            return {
                "total_quotas": sum(len(v) for v in self._quotas.values()),
                "scopes_tracked": len(self._quotas),
                "currently_exceeded": total_exceeded,
                "by_scope": scopes,
            }
