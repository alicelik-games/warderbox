"""Execution quotas for agent tool calls.

Enforces daily, weekly, and monthly call limits per tool or agent,
complementing the sliding-window rate limiter with longer-term caps.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from warderbox.state import StateBackend


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

    def __init__(
        self,
        persist_path: str | None = None,
        backend: StateBackend | None = None,
        namespace: str = "default",
    ) -> None:
        if persist_path and backend is not None:
            raise ValueError("Use either persist_path or backend, not both")
        self._backend = backend
        self._namespace = namespace
        self._quotas: dict[str, list[QuotaUsage]] = {}
        self._lock = threading.Lock()
        # Optional durable backend: when set, quota counters survive process
        # restarts so long-period (e.g. monthly) caps are not reset on reboot.
        self._persist_path = persist_path
        if persist_path:
            self._load()

    def use_backend(self, backend: StateBackend, namespace: str = "default") -> None:
        """Keep counters in shared state so quotas hold across workers."""
        if self._persist_path:
            raise ValueError("Use either persist_path or backend, not both")
        self._backend = backend
        self._namespace = namespace

    @staticmethod
    def _counter_key(usage: QuotaUsage, scope: str) -> str:
        return f"quota:{scope}:{usage.limit.period.value}"

    def _applicable(self, tool_name: str) -> list[tuple[str, QuotaUsage]]:
        return [
            (scope, usage)
            for scope in (tool_name, "global")
            for usage in self._quotas.get(scope, [])
        ]

    def _counter_checks(
        self, applicable: list[tuple[str, QuotaUsage]]
    ) -> list[tuple[str, int, float]]:
        return [
            (
                self._counter_key(usage, scope),
                usage.limit.max_calls,
                float(PERIOD_SECONDS[usage.limit.period]),
            )
            for scope, usage in applicable
        ]

    def set_quota(
        self,
        scope: str,
        period: QuotaPeriod,
        max_calls: int,
    ) -> None:
        """Set a quota for a scope (tool name, agent name, or 'global').

        Re-declaring an existing scope+period keeps the current counter and
        window so that persisted usage is not reset when an app re-registers
        its quotas on startup.
        """
        limit = QuotaLimit(period=period, max_calls=max_calls, scope=scope)
        with self._lock:
            existing = self._quotas.get(scope, [])
            prior = next((u for u in existing if u.limit.period == period), None)
            if prior is not None:
                usage = QuotaUsage(
                    limit=limit,
                    current_calls=prior.current_calls,
                    window_start=prior.window_start,
                )
            else:
                usage = QuotaUsage(limit=limit)
            others = [u for u in existing if u.limit.period != period]
            others.append(usage)
            self._quotas[scope] = others
            self._save()

    def remove_quota(self, scope: str, period: QuotaPeriod | None = None) -> bool:
        """Remove quota(s) for a scope."""
        with self._lock:
            if scope not in self._quotas:
                return False
            if period is None:
                del self._quotas[scope]
                self._save()
                return True
            before = len(self._quotas[scope])
            self._quotas[scope] = [
                u for u in self._quotas[scope] if u.limit.period != period
            ]
            if not self._quotas[scope]:
                del self._quotas[scope]
            self._save()
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

    def reserve(self, tool_name: str) -> tuple[bool, str | None]:
        """Atomically check and increment quotas for a tool.

        Returns (False, reason) without incrementing if any applicable quota
        is already at its limit; otherwise increments every applicable quota
        and returns (True, None). This closes the check-then-record race where
        two concurrent calls could both pass a limit-of-one quota.

        With a backend the quota holds across workers, and this raises
        StateBackendError if the backend cannot be reached.
        """
        with self._lock:
            if self._backend is not None:
                applicable = self._applicable(tool_name)
                if not applicable:
                    return True, None
                violated = self._backend.counter_reserve(
                    self._namespace, self._counter_checks(applicable)
                )
                if violated is None:
                    return True, None
                scope, usage = applicable[violated[0]]
                return False, (
                    f"Quota exceeded for '{scope}': "
                    f"{violated[1]}/{usage.limit.max_calls} "
                    f"{usage.limit.period.value}"
                )
            now = time.time()
            for scope in (tool_name, "global"):
                for usage in self._quotas.get(scope, []):
                    self._maybe_reset(usage, now)
                    if usage.current_calls >= usage.limit.max_calls:
                        return False, (
                            f"Quota exceeded for '{scope}': "
                            f"{usage.current_calls}/{usage.limit.max_calls} "
                            f"{usage.limit.period.value}"
                        )
            changed = False
            for scope in (tool_name, "global"):
                for usage in self._quotas.get(scope, []):
                    usage.current_calls += 1
                    changed = True
            if changed:
                self._save()
            return True, None

    def release(self, tool_name: str) -> None:
        """Roll back a reservation (e.g. dry-run or deduplicated call)."""
        with self._lock:
            if self._backend is not None:
                self._backend.counter_release(self._namespace, [
                    self._counter_key(usage, scope)
                    for scope, usage in self._applicable(tool_name)
                ])
                return
            changed = False
            for scope in (tool_name, "global"):
                for usage in self._quotas.get(scope, []):
                    if usage.current_calls > 0:
                        usage.current_calls -= 1
                        changed = True
            if changed:
                self._save()

    def record(self, tool_name: str) -> None:
        """Record a tool execution against all applicable quotas."""
        with self._lock:
            if self._backend is not None:
                self._backend.counter_add(
                    self._namespace,
                    self._counter_checks(self._applicable(tool_name)),
                )
                return
            now = time.time()
            changed = False
            for scope in (tool_name, "global"):
                usages = self._quotas.get(scope, [])
                for usage in usages:
                    self._maybe_reset(usage, now)
                    usage.current_calls += 1
                    changed = True
            if changed:
                self._save()

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
        if self._backend is not None:
            # Shared counters are authoritative; refresh the local view.
            usage.current_calls, usage.window_start = self._backend.counter_get(
                self._namespace,
                self._counter_key(usage, usage.limit.scope),
                float(PERIOD_SECONDS[usage.limit.period]),
            )
            return
        window = PERIOD_SECONDS[usage.limit.period]
        if now - usage.window_start >= window:
            usage.current_calls = 0
            usage.window_start = now

    def _save(self) -> None:
        """Persist quota state (called while holding the lock)."""
        if not self._persist_path:
            return
        data = {
            scope: [
                {
                    "period": u.limit.period.value,
                    "max_calls": u.limit.max_calls,
                    "scope": u.limit.scope,
                    "current_calls": u.current_calls,
                    "window_start": u.window_start,
                }
                for u in usages
            ]
            for scope, usages in self._quotas.items()
        }
        try:
            Path(self._persist_path).write_text(json.dumps(data))
        except OSError:
            pass

    def _load(self) -> None:
        try:
            data = json.loads(Path(self._persist_path).read_text())
        except (OSError, ValueError):
            return
        for scope, items in data.items():
            usages = []
            for it in items:
                try:
                    period = QuotaPeriod(it["period"])
                except ValueError:
                    continue
                limit = QuotaLimit(
                    period=period,
                    max_calls=it["max_calls"],
                    scope=it.get("scope", scope),
                )
                usages.append(QuotaUsage(
                    limit=limit,
                    current_calls=it.get("current_calls", 0),
                    window_start=it.get("window_start", time.time()),
                ))
            if usages:
                self._quotas[scope] = usages

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
