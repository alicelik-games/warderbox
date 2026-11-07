"""Retries work after a timeout without running a slow tool twice at once."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from warderbox import AsyncWarderBox, RetryPolicy, WarderBox
from warderbox.timeout import TimeoutError as ToolTimeoutError
from warderbox.timeout import TimeoutExecutor, TimeoutPolicy


def _policy(**kwargs):
    return RetryPolicy(max_retries=2, backoff_base=0, jitter=0, **kwargs)


def _sync(seconds, policy):
    shield = WarderBox(budget=5.0)
    if seconds:
        shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=seconds)))

    def wrap(func):
        return shield.tool(cost=0.01, retry=policy)(func)

    return wrap


def _async(seconds, policy):
    shield = AsyncWarderBox(budget=5.0)
    if seconds:
        shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=seconds)))

    def wrap(func):
        tool = shield.async_tool(cost=0.01, retry=policy)(func)
        return lambda: asyncio.run(tool())

    return wrap


BUILDERS = pytest.mark.parametrize("build", [_sync, _async], ids=["sync", "async"])


@BUILDERS
def test_a_timeout_raised_by_the_tool_is_retried(build):
    calls = []

    def tool():
        calls.append(1)
        raise TimeoutError("t/o")

    result = build(None, _policy())(tool)()
    assert result.error
    assert len(calls) == 3


@BUILDERS
def test_recovers_when_a_retry_succeeds_after_a_tool_timeout(build):
    calls = []

    def tool():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("t/o")
        return "ok"

    result = build(None, _policy())(tool)()
    assert result.output == "ok"
    assert result.error is None


@BUILDERS
def test_retry_on_decides_for_timeouts_too(build):
    calls = []

    def tool():
        calls.append(1)
        raise TimeoutError("t/o")

    build(None, _policy(retry_on=lambda e: False))(tool)()
    assert len(calls) == 1


@BUILDERS
def test_slow_tool_is_not_run_twice_and_its_late_result_is_used(build):
    calls, running, peak = [], [], []

    def tool():
        calls.append(1)
        running.append(1)
        peak.append(len(running))
        time.sleep(0.15)
        running.pop()
        return "late"

    result = build(0.1, _policy())(tool)()
    assert result.output == "late"
    assert result.error is None
    assert len(calls) == 1
    assert max(peak) == 1


@BUILDERS
def test_all_attempts_waiting_on_a_hung_tool_report_the_timeout(build):
    calls = []
    release = threading.Event()

    def tool():
        calls.append(1)
        release.wait(2)

    result = build(0.02, _policy())(tool)()
    release.set()
    assert "timeout" in result.error.lower()
    assert len(calls) == 1


@BUILDERS
def test_late_failure_of_the_first_attempt_starts_a_real_retry(build):
    calls = []

    def tool():
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.1)
            raise ValueError("first failed")
        return "second"

    result = build(0.07, _policy())(tool)()
    assert result.output == "second"
    assert len(calls) == 2


@BUILDERS
def test_overlap_on_timeout_starts_a_new_attempt_for_a_hung_tool(build):
    calls = []
    release = threading.Event()

    def tool():
        calls.append(1)
        if len(calls) == 1:
            release.wait(2)
            return "stuck"
        return "fresh"

    result = build(0.03, _policy(overlap_on_timeout=True))(tool)()
    release.set()
    assert result.output == "fresh"
    assert len(calls) == 2


def test_coroutine_is_cancelled_at_the_deadline_and_retried_fresh():
    calls, finished = [], []

    async def tool():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(1)
            finished.append(1)
        return "ok"

    shield = AsyncWarderBox(budget=5.0)
    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=0.02)))
    wrapped = shield.async_tool(cost=0.01, retry=_policy())(tool)
    result = asyncio.run(wrapped())
    assert result.output == "ok"
    assert len(calls) == 2
    assert not finished


def test_a_call_without_retry_does_not_keep_the_attempt():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=0.01))
    calls = []

    def tool():
        calls.append(1)
        time.sleep(0.05)

    for _ in range(2):
        with pytest.raises(ToolTimeoutError):
            executor.execute("t", tool)
    assert len(calls) == 2
