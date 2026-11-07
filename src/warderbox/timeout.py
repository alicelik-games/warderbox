"""Execution deadlines for agent tool calls.

Two modes, chosen with ``TimeoutPolicy(isolation=...)``:

``"thread"`` (default)
    The caller stops waiting when the deadline passes and gets a timeout
    result, but Python cannot kill a thread: **the tool keeps running in the
    background** and its side effect may still land. Use it for read-only tools
    and for keeping the agent responsive. It is not a safety boundary.

``"process"``
    The tool runs in a forked child that is terminated at the deadline, so the
    work really stops. The trade-offs are inherent to process isolation: only
    available where ``fork`` exists (not Windows), the return value and any
    exception must be picklable, and anything the tool mutates in memory
    (globals, closures, connections opened by the parent) stays in the child.
    A side effect already sent to an external system before the kill cannot be
    recalled. Forking a multi-threaded process is inherently delicate (Python
    3.12+ warns about it): keep process-isolated tools self-contained.

``process_tools`` selects process isolation for individual tools.
"""

from __future__ import annotations

import asyncio
import inspect
import multiprocessing
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
    isolation: str = "thread"
    process_tools: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.isolation not in ("thread", "process"):
            raise ValueError(
                f"isolation must be 'thread' or 'process', got {self.isolation!r}"
            )
        if self.isolation == "process" or self.process_tools:
            _fork_context()

    def get_timeout(self, tool_name: str) -> float:
        return self.tool_timeouts.get(tool_name, self.default_seconds)

    def isolates(self, tool_name: str) -> bool:
        return self.isolation == "process" or tool_name in self.process_tools


def _fork_context() -> Any:
    try:
        return multiprocessing.get_context("fork")
    except ValueError as e:
        raise ValueError(
            "Process isolation needs the 'fork' start method, which this "
            "platform does not provide. Use isolation='thread'."
        ) from e


def _child_main(conn: Any, func: Callable, args: tuple, kwargs: dict) -> None:
    try:
        value = func(*args, **kwargs)
        if inspect.isawaitable(value):
            async def _await() -> Any:
                return await value
            value = asyncio.run(_await())
        message = ("ok", value)
    except BaseException as exc:
        message = ("error", exc)
    try:
        conn.send(message)
    except Exception as exc:
        # Result or exception could not be pickled.
        conn.send(("error", RuntimeError(
            f"Process-isolated tool produced an unpicklable "
            f"{message[0]} value: {exc}. Original: {message[1]!r}"
        )))
    finally:
        conn.close()


class InFlight:
    """The attempt of one logical call still running after its deadline.

    A retry takes it over instead of starting the tool a second time, so a slow
    call is never running twice at once and a late result is not lost.
    """

    def __init__(self) -> None:
        self._attempt: Any = None

    def keep(self, attempt: Any) -> None:
        self._attempt = attempt

    def take(self) -> Any:
        attempt, self._attempt = self._attempt, None
        return attempt


class _ThreadAttempt:
    def __init__(self, func: Callable, args: tuple, kwargs: dict) -> None:
        self.result: list[Any] = []
        self.error: list[BaseException] = []

        def target() -> None:
            # BaseException too: a KeyboardInterrupt or SystemExit raised by the
            # tool must reach the caller, not look like a None result.
            try:
                self.result.append(func(*args, **kwargs))
            except BaseException as exc:
                self.error.append(exc)

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()


class TimeoutExecutor:
    """Run callables under a deadline. See the module docstring for modes."""

    def __init__(self, policy: TimeoutPolicy | None = None) -> None:
        self._policy = policy or TimeoutPolicy()
        self._timed_out_count = 0
        self._killed_count = 0
        self._total_count = 0
        self._count_lock = threading.Lock()

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
        inflight: InFlight | None = None,
    ) -> Any:
        """Run *func* with a deadline. Raises TimeoutError on expiry.

        With *inflight*, a thread-mode attempt still running at the deadline is
        kept there, and the next call with it resumes waiting for that attempt.
        """
        kwargs = kwargs or {}
        timeout = timeout_override or self._policy.get_timeout(tool_name)
        with self._count_lock:
            self._total_count += 1

        if self._policy.isolates(tool_name):
            return self._execute_in_process(tool_name, func, args, kwargs, timeout)
        return self._execute_in_thread(
            tool_name, func, args, kwargs, timeout, inflight
        )

    def _execute_in_thread(
        self,
        tool_name: str,
        func: Callable,
        args: tuple,
        kwargs: dict,
        timeout: float,
        inflight: InFlight | None = None,
    ) -> Any:
        attempt = inflight.take() if inflight else None
        if attempt is None:
            attempt = _ThreadAttempt(func, args, kwargs)
        attempt.thread.join(timeout=timeout)

        if attempt.thread.is_alive():
            if inflight is not None:
                inflight.keep(attempt)
            with self._count_lock:
                self._timed_out_count += 1
            raise TimeoutError(
                f"Tool '{tool_name}' exceeded {timeout}s timeout "
                f"(the tool may still be running in the background)"
            )

        if attempt.error:
            raise attempt.error[0]

        return attempt.result[0] if attempt.result else None

    def _execute_in_process(
        self, tool_name: str, func: Callable, args: tuple, kwargs: dict, timeout: float
    ) -> Any:
        ctx = _fork_context()
        receiver, sender = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_child_main, args=(sender, func, args, kwargs), daemon=True
        )
        proc.start()
        sender.close()
        try:
            if not receiver.poll(timeout):
                proc.terminate()
                proc.join(1.0)
                if proc.is_alive():
                    proc.kill()
                    proc.join(1.0)
                with self._count_lock:
                    self._timed_out_count += 1
                    self._killed_count += 1
                raise TimeoutError(
                    f"Tool '{tool_name}' exceeded {timeout}s timeout "
                    f"(worker process terminated)"
                )
            try:
                status, payload = receiver.recv()
            except EOFError:
                proc.join(1.0)
                raise RuntimeError(
                    f"Process-isolated tool '{tool_name}' died without a "
                    f"result (exit code {proc.exitcode})"
                ) from None
            proc.join(1.0)
        except BaseException:
            if proc.is_alive():
                proc.terminate()
                proc.join(1.0)
            raise
        finally:
            receiver.close()
        if status == "error":
            if isinstance(payload, Exception):
                raise payload
            raise RuntimeError(
                f"Process-isolated tool '{tool_name}' raised {payload!r}"
            )
        return payload

    def stats(self) -> dict:
        return {
            "total_executions": self._total_count,
            "timed_out": self._timed_out_count,
            "killed": self._killed_count,
            "default_timeout": self._policy.default_seconds,
            "tool_overrides": len(self._policy.tool_timeouts),
            "isolation": self._policy.isolation,
        }
