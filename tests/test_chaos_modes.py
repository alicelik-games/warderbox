"""Chaos injection is gated identically on the sync and async paths."""

from __future__ import annotations

import asyncio
import time
import warnings

import pytest

from warderbox import AsyncWarderBox, ChaosEngine, CircuitBreaker, FaultType, WarderBox


def _chaos(fault=FaultType.ERROR, **kwargs):
    kwargs.setdefault("allow_production", True)
    engine = ChaosEngine(**kwargs)
    engine.add_fault("tool", fault, latency_ms=1)
    return engine


def _sync(mode, chaos):
    shield = WarderBox(budget=1.0, mode=mode)
    shield.set_chaos(chaos)
    ran = []

    @shield.tool(cost=0.01)
    def tool():
        ran.append(1)
        return "real"

    return shield, tool, ran


def _async(mode, chaos):
    shield = AsyncWarderBox(budget=1.0, mode=mode)
    shield.set_chaos(chaos)
    ran = []

    @shield.async_tool(cost=0.01)
    async def tool():
        ran.append(1)
        return "real"

    return shield, tool, ran


def test_attached_engine_fires_in_production_on_both_paths():
    _s, tool, ran = _sync("production", _chaos())
    assert "[CHAOS]" in tool().error and not ran

    _s, tool, ran = _async("production", _chaos())
    assert "[CHAOS]" in asyncio.run(tool()).error and not ran


def test_allow_production_false_keeps_engine_inert_on_both_paths():
    _s, tool, ran = _sync("production", _chaos(allow_production=False))
    assert tool().output == "real" and ran

    _s, tool, ran = _async("production", _chaos(allow_production=False))
    assert asyncio.run(tool()).output == "real" and ran


def test_attaching_in_production_warns_but_test_mode_does_not():
    with pytest.warns(RuntimeWarning, match="production"):
        WarderBox(budget=1.0).set_chaos(ChaosEngine(allow_production=True))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        WarderBox(budget=1.0, mode="test").set_chaos(ChaosEngine())
        WarderBox(budget=1.0).set_chaos(ChaosEngine())
        WarderBox(budget=1.0).set_chaos(ChaosEngine(allow_production=False))


def test_test_mode_injects_without_running_the_tool():
    for build, run in (
        (_sync, lambda t: t()),
        (_async, lambda t: asyncio.run(t())),
    ):
        _s, tool, ran = build("test", _chaos())
        result = run(tool)
        assert "[CHAOS]" in result.error
        assert not ran


def test_test_mode_fault_reaches_the_circuit_breaker_without_spending():
    shield, tool, _ran = _sync("test", _chaos())
    breaker = CircuitBreaker(failure_threshold=1)
    shield.set_circuit_breaker(breaker)
    tool()
    assert not breaker.should_allow("tool")[0]
    assert shield.budget_summary["spent"] == 0
    assert shield.budget_summary["active_reservations"] == 0


@pytest.mark.parametrize(("build", "run"), [
    (_sync, lambda t: t()),
    (_async, lambda t: asyncio.run(t())),
])
def test_timeout_fault_fires_timeout_event_on_both_paths(build, run):
    shield, tool, _ran = build("production", _chaos(
        FaultType.TIMEOUT, allow_production=True
    ))
    events = []
    shield.notifier.on("action_timeout", lambda e, d: events.append(d))
    assert "timeout" in run(tool).error.lower()
    assert events


def test_corrupt_output_changes_output_on_both_paths():
    _s, tool, ran = _sync("production", _chaos(
        FaultType.CORRUPT_OUTPUT, allow_production=True
    ))
    assert tool().output == "[CORRUPTED] laer" and ran

    _s, tool, ran = _async("production", _chaos(
        FaultType.CORRUPT_OUTPUT, allow_production=True
    ))
    assert asyncio.run(tool()).output == "[CORRUPTED] laer" and ran

    _s, tool, ran = _sync("test", _chaos(FaultType.CORRUPT_OUTPUT))
    assert tool().output.startswith("[CORRUPTED]") and not ran


def test_async_latency_does_not_freeze_the_event_loop():
    engine = ChaosEngine(allow_production=True)
    engine.add_fault("tool", FaultType.LATENCY, latency_ms=200)
    _s, tool, _ran = _async("production", engine)

    async def main():
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await tool()
        task.cancel()
        return ticks

    assert len(asyncio.run(main())) > 5
