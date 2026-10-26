"""Execution timeout enforcement for agent tool calls.

Wraps tool execution with a hard timeout so hanging calls don't
block the agent forever or burn budget silently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class TimeoutError(Exception):
    """Raised when a tool call exceeds its allowed execution time."""


@dataclass
class TimeoutPolicy:
    """Timeout configuration for a tool or globally."""

    default_seconds: float = 30.0
    tool_timeouts: dict[str, float] = field(default_factory=dict)

    def get_timeout(self, tool_name: str) -> float:
        return self.tool_timeouts.get(tool_name, self.default_seconds)


class TimeoutExecutor:
    """Execute callables with a hard timeout.

    Uses threading to enforce the timeout — the call runs in a
    daemon thread and the caller blocks until it finishes or the
    deadline elapses.
    """

    def __init__(self, policy: TimeoutPolicy | None = None) -> None:
        self._policy = policy or TimeoutPolicy()
        self._timed_out_count = 0
        self._total_count = 0

    @property
    def policy(self) -> TimeoutPolicy:
        return self._policy

    def execute(
        self,
        tool_name: str,
        func: Callable,
        args: tuple = (),
        kwargs: dict | None = None,
        timeout_override: float | None = None,
    ) -> Any:
        """Run *func* with a timeout. Raises TimeoutError on expiry."""
        kwargs = kwargs or {}
        timeout = timeout_override or self._policy.get_timeout(tool_name)
        self._total_count += 1

        result_box: list[Any] = []
        error_box: list[Exception] = []

        def target() -> None:
            try:
                result_box.append(func(*args, **kwargs))
            except Exception as exc:
                error_box.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            self._timed_out_count += 1
            raise TimeoutError(
                f"Tool '{tool_name}' exceeded {timeout}s timeout"
            )

        if error_box:
            raise error_box[0]

        return result_box[0] if result_box else None

    def stats(self) -> dict:
        return {
            "total_executions": self._total_count,
            "timed_out": self._timed_out_count,
            "default_timeout": self._policy.default_seconds,
            "tool_overrides": len(self._policy.tool_timeouts),
        }
