"""Hardening regression tests."""

from __future__ import annotations

import asyncio
import tempfile
import warnings
from pathlib import Path

from warderbox import (
    AnomalyDetector,
    AsyncWarderBox,
    CircuitBreaker,
    FallbackManager,
    RiskLevel,
    WarderBox,
)


def _fb(**kw):
    s = WarderBox(budget=5.0, **kw)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    return s, cb, FallbackManager()


# #1 test mode must not execute the fallback
def test_g1_test_mode_fallback_not_executed():
    s, cb, fb = _fb(mode="test")
    ran = []
    fb.register("primary", "secondary",
                fallback_fn=lambda **k: (ran.append(1) or "fb"), fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    primary()
    assert ran == []


# #2 fallback must honor the per-tool risk floor
def test_g2_fallback_respects_risk_floor():
    s, cb, fb = _fb()
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01, risk=RiskLevel.CRITICAL)
    def primary():
        return "p"

    cb.record_failure("primary")
    assert primary().blocked is True


# #3 a cancelled async task does not leak budget/rate/quota reservations.
# (Idempotency-marker retention on cancel is covered more precisely by the
# fifth-audit state-machine tests: released if execution never started,
# kept if it had — see test_h1/test_h1b.)
def test_g3_async_cancel_releases_reservations():
    a = AsyncWarderBox(budget=1.0)

    @a.async_tool(cost=0.4)
    async def slow(x: int):
        await asyncio.sleep(1.0)
        return "ok"

    async def main():
        t = asyncio.create_task(slow(x=1))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return a.budget_summary["reserved"]

    assert asyncio.run(main()) == 0.0


# #4 load_history_from_db is idempotent on budget
def test_g4_load_history_idempotent_budget():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "a.db")
        s = WarderBox(budget=10.0, db_path=db)

        @s.tool(cost=1.0)
        def act():
            return "ok"

        act()
        act()
        s._logger.close()

        fresh = WarderBox(db_path=db)
        fresh.load_history_from_db()
        fresh.load_history_from_db()  # second call must not double count
        assert abs(fresh.budget_summary["spent"] - 2.0) < 1e-6


# #5 @shield.tool on an async function does not report success with a coroutine
def test_g5_sync_tool_async_func_not_success():
    s = WarderBox(budget=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        @s.tool(cost=0.01)
        async def acor():
            return "x"

    r = acor()
    assert r.success is False
    assert not hasattr(r.output, "__await__")


# #6 an anomaly detector/callback error must not crash the tool call
def test_g6_anomaly_error_isolated():
    class ExplodingDetector:
        def record(self, tool_name, cost, timestamp=None):
            raise RuntimeError("detector boom")

    s = WarderBox(budget=1000.0)
    s.set_anomaly_detector(ExplodingDetector())

    @s.tool(cost=1.0)
    def go():
        return "ok"

    r = go()
    assert r.success is True


# #6b a real anomaly alert callback that raises is also isolated
def test_g6b_anomaly_callback_error_isolated():
    det = AnomalyDetector(window_size=3, sensitivity=0.5)

    def boom(alert):
        raise RuntimeError("callback boom")

    det.set_callback(boom)
    s = WarderBox(budget=1000.0)
    s.set_anomaly_detector(det)

    @s.tool(cost=1.0)
    def go():
        return "ok"

    for _ in range(6):
        r = go()
    assert r.success is True


# #7 sensitive numeric values are masked
def test_g7_numeric_sensitive_key_masked():
    from warderbox.masking import DataMasker
    m = DataMasker()
    out = m.mask({"ssn": 123456789})
    assert out["ssn"] != 123456789
