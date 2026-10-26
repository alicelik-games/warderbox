"""Tests for execution timeout enforcement."""

import time

import pytest

from warderbox.timeout import TimeoutError, TimeoutExecutor, TimeoutPolicy


def test_normal_execution():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=5.0))
    result = executor.execute("tool_a", lambda: 42)
    assert result == 42


def test_timeout_raises():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=0.1))
    with pytest.raises(TimeoutError, match="exceeded"):
        executor.execute("slow_tool", lambda: time.sleep(5))


def test_per_tool_timeout():
    policy = TimeoutPolicy(
        default_seconds=5.0,
        tool_timeouts={"fast_tool": 0.1},
    )
    executor = TimeoutExecutor(policy)
    result = executor.execute("normal_tool", lambda: "ok")
    assert result == "ok"

    with pytest.raises(TimeoutError):
        executor.execute("fast_tool", lambda: time.sleep(5))


def test_timeout_override():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=5.0))
    with pytest.raises(TimeoutError):
        executor.execute("tool", lambda: time.sleep(5), timeout_override=0.1)


def test_func_exception_propagates():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=5.0))
    with pytest.raises(ValueError, match="boom"):
        executor.execute("tool", lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_func_with_args():
    executor = TimeoutExecutor()
    result = executor.execute("add", lambda a, b: a + b, args=(3, 4))
    assert result == 7


def test_func_with_kwargs():
    executor = TimeoutExecutor()
    result = executor.execute(
        "greet", lambda name="world": f"hello {name}",
        kwargs={"name": "ali"},
    )
    assert result == "hello ali"


def test_stats():
    executor = TimeoutExecutor(TimeoutPolicy(
        default_seconds=5.0,
        tool_timeouts={"a": 1.0, "b": 2.0},
    ))
    executor.execute("a", lambda: 1)
    executor.execute("b", lambda: 2)
    stats = executor.stats()
    assert stats["total_executions"] == 2
    assert stats["timed_out"] == 0
    assert stats["tool_overrides"] == 2


def test_stats_after_timeout():
    executor = TimeoutExecutor(TimeoutPolicy(default_seconds=0.1))
    try:
        executor.execute("slow", lambda: time.sleep(5))
    except TimeoutError:
        pass
    assert executor.stats()["timed_out"] == 1


def test_policy_get_timeout():
    policy = TimeoutPolicy(default_seconds=10.0, tool_timeouts={"x": 3.0})
    assert policy.get_timeout("x") == 3.0
    assert policy.get_timeout("y") == 10.0


def test_none_return():
    executor = TimeoutExecutor()
    result = executor.execute("noop", lambda: None)
    assert result is None
