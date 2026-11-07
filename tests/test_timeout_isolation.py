"""Thread timeouts stop waiting; process timeouts stop the work."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from warderbox import AsyncWarderBox, RetryPolicy, WarderBox
from warderbox.timeout import TimeoutError as ToolTimeoutError
from warderbox.timeout import TimeoutExecutor, TimeoutPolicy

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")


def _slow_writer(path):
    time.sleep(0.4)
    with open(path, "w") as f:
        f.write("ran")
    return "done"


def test_thread_mode_leaves_the_tool_running(tmp_path):
    marker = tmp_path / "m"
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=0.05))
    with pytest.raises(ToolTimeoutError, match="may still be running"):
        executor.execute("t", _slow_writer, (str(marker),))
    time.sleep(0.6)
    assert marker.exists()


def test_process_mode_stops_the_tool(tmp_path):
    marker = tmp_path / "m"
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=0.05, isolation="process"))
    with pytest.raises(ToolTimeoutError, match="terminated"):
        executor.execute("t", _slow_writer, (str(marker),))
    time.sleep(0.6)
    assert not marker.exists()
    assert executor.stats()["killed"] == 1


def test_process_tools_selects_individual_tools(tmp_path):
    executor = TimeoutExecutor(TimeoutPolicy(
        default_seconds=0.05, process_tools={"dangerous"}
    ))
    marker = tmp_path / "m"
    with pytest.raises(ToolTimeoutError, match="terminated"):
        executor.execute("dangerous", _slow_writer, (str(marker),))
    with pytest.raises(ToolTimeoutError, match="may still be running"):
        executor.execute("other", _slow_writer, (str(tmp_path / "n"),))


def test_process_mode_returns_values_and_exceptions():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=5, isolation="process"))
    assert executor.execute("t", lambda a, b=0: a + b, (1,), {"b": 2}) == 3

    def boom():
        raise KeyError("bad")

    with pytest.raises(KeyError):
        executor.execute("t", boom)


def test_process_mode_reports_unpicklable_results():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=5, isolation="process"))
    with pytest.raises(RuntimeError, match="unpicklable"):
        executor.execute("t", lambda: (lambda: 1))


def test_process_mode_reports_a_dead_worker():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=5, isolation="process"))
    with pytest.raises(RuntimeError, match="died"):
        executor.execute("t", lambda: os._exit(7))


def test_invalid_isolation_is_rejected():
    with pytest.raises(ValueError):
        TimeoutPolicy(isolation="magic")


def test_shield_process_timeout_stops_work_and_retry_starts_fresh(tmp_path):
    marker = tmp_path / "m"
    shield = WarderBox(budget=1.0)
    shield.set_timeout(TimeoutExecutor(
        TimeoutPolicy(default_seconds=0.05, isolation="process")
    ))
    starts = tmp_path / "starts"

    @shield.tool(cost=0.01, retry=RetryPolicy(max_retries=2, backoff_base=0, jitter=0))
    def slow():
        with open(starts, "a") as f:
            f.write("x")
        _slow_writer(str(marker))

    result = slow()
    assert "terminated" in result.error
    time.sleep(0.6)
    assert not marker.exists()
    # The old attempt was killed, so each retry is safe to start from scratch.
    assert starts.read_text() == "xxx"


def test_async_shield_process_timeout_stops_work(tmp_path):
    marker = tmp_path / "m"
    shield = AsyncWarderBox(budget=1.0)
    shield.set_timeout(TimeoutExecutor(
        TimeoutPolicy(default_seconds=0.05, isolation="process")
    ))
    events = []
    shield.notifier.on("action_timeout", lambda e, d: events.append(d))

    @shield.async_tool(cost=0.01)
    async def slow():
        await asyncio.sleep(0.4)
        marker.write_text("ran")

    result = asyncio.run(slow())
    assert "terminated" in result.error
    assert events
    time.sleep(0.6)
    assert not marker.exists()


def test_async_shield_process_mode_returns_coroutine_result():
    shield = AsyncWarderBox(budget=1.0)
    shield.set_timeout(TimeoutExecutor(
        TimeoutPolicy(default_seconds=5, isolation="process")
    ))

    @shield.async_tool(cost=0.01)
    async def fast():
        await asyncio.sleep(0)
        return "value"

    assert asyncio.run(fast()).output == "value"
