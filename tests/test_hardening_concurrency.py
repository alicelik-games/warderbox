"""Hardening regression tests."""

from __future__ import annotations

import threading
import time

import pytest

from warderbox import (
    ApprovalManager,
    AsyncWarderBox,
    CapabilityManager,
    CircuitBreaker,
    FallbackManager,
    IdempotencyGuard,
    RiskLevel,
    WarderBox,
)
from warderbox.quota import QuotaManager, QuotaPeriod


# R3 — risk override must not suppress a higher detected risk
def test_r3_risk_low_does_not_suppress_critical():
    s = WarderBox(budget=5.0)

    @s.tool(cost=0.01, risk=RiskLevel.LOW)
    def q(sql: str) -> str:
        return "EXECUTED"

    r = q(sql="DROP TABLE users")
    assert r.blocked is True
    assert r.output != "EXECUTED"


def test_r3_override_can_still_raise():
    s = WarderBox(budget=5.0)

    @s.tool(cost=0.01, risk=RiskLevel.CRITICAL)
    def danger() -> str:
        return "x"

    assert danger().blocked is True


# R1 — fallback must honor capability and approval
def _fallback_shield(**kw):
    s = WarderBox(budget=5.0, **kw)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()
    return s, cb, fb


def test_r1_fallback_respects_capability():
    caps = CapabilityManager()
    caps.grant("bot", allow=["primary"])  # secondary NOT allowed
    s, cb, fb = _fallback_shield(agent_name="bot")
    s.set_capabilities(caps)
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary() -> str:
        return "p"

    cb.record_failure("primary")
    assert primary().blocked is True


def test_r1_fallback_respects_approval():
    ap = ApprovalManager(default_timeout=0.1)
    ap.require_approval("primary", auto_deny=True)
    s, cb, fb = _fallback_shield()
    s.set_approval(ap)
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary() -> str:
        return "p"

    cb.record_failure("primary")
    assert primary().blocked is True


# R2 — idempotency atomic across caller retry and concurrency
def test_r2_caller_retry_not_double_sent():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    s = WarderBox(budget=5.0)
    s.set_idempotency(guard)

    sent = []

    @s.tool(cost=0.01)
    def send_email(to: str) -> str:
        sent.append(to)
        if len(sent) == 1:
            raise RuntimeError("committed then timed out")
        return "sent"

    send_email(to="a@b.c")   # fails after side effect
    second = send_email(to="a@b.c")  # caller retry
    assert len(sent) == 1
    assert second.blocked is True


def test_r2_concurrent_not_double_executed():
    guard = IdempotencyGuard()
    guard.add_protected_tool("charge")
    s = WarderBox(budget=100.0)
    s.set_idempotency(guard)

    calls = []

    @s.tool(cost=0.01)
    def charge(amount: int) -> str:
        calls.append(amount)
        time.sleep(0.05)
        return "ok"

    # Both threads line up BEFORE invoking, so they race on begin().
    start = threading.Barrier(2)
    results = []

    def worker():
        start.wait()
        results.append(charge(amount=5))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert sum(1 for r in results if r.blocked) == 1


# R8 — async approval non-blocking + async fallback awaited
def test_r8_async_approval_denied_blocks():
    import asyncio
    ap = ApprovalManager(default_timeout=0.1)
    ap.require_approval("send", auto_deny=True)
    s = AsyncWarderBox(budget=5.0)
    s.set_approval(ap)

    sent = []

    @s.async_tool(cost=0.01)
    async def send(to: str) -> str:
        sent.append(to)
        return "sent"

    r = asyncio.run(send(to="x@y.com"))
    assert r.blocked is True
    assert sent == []


def test_r8_async_approval_does_not_freeze_loop():
    import asyncio
    ap = ApprovalManager(default_timeout=0.3)
    ap.require_approval("slow_approve", auto_deny=True)
    s = AsyncWarderBox(budget=5.0)
    s.set_approval(ap)

    @s.async_tool(cost=0.01)
    async def slow_approve() -> str:
        return "ok"

    ticks = []

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks.append(1)

    async def main():
        await asyncio.gather(slow_approve(), ticker())

    asyncio.run(main())
    # If approval blocked the loop, the ticker could not run concurrently.
    assert len(ticks) == 5


def test_r8_async_fallback_awaited():
    import asyncio
    s = AsyncWarderBox(budget=5.0)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()

    async def afb(**kwargs):
        await asyncio.sleep(0)
        return "async-fallback-ran"

    fb.register("primary", "secondary", fallback_fn=afb, fallback_cost=0.01)
    s.set_fallback(fb)

    @s.async_tool(cost=0.01)
    async def primary() -> str:
        return "p"

    cb.record_failure("primary")
    r = asyncio.run(primary())
    assert r.output == "async-fallback-ran"


# R6 — forecast burn rate uses a floored window
def test_r6_forecast_floored_window():
    from warderbox.forecast import CostForecaster
    s = WarderBox(budget=100.0)

    @s.tool(cost=1.0)
    def act() -> str:
        return "ok"

    for _ in range(3):   # three fast consecutive calls
        act()

    report = CostForecaster(s).forecast()
    # 3 * $1 over a >=60s floored window => <= 180/hr, not millions.
    assert report.time_window_seconds >= 60.0
    assert report.burn_rate_per_hour <= 3 * 1.0 / 60.0 * 3600 + 1


# R7 — dashboard history load rebuilds budget / FinOps
def test_r7_load_history_rebuilds_budget(tmp_path):
    from warderbox.forecast import CostForecaster
    db = str(tmp_path / "a.db")
    s = WarderBox(budget=10.0, db_path=db)

    @s.tool(cost=1.0)
    def act() -> str:
        return "ok"

    act()
    act()
    s._logger.close()

    fresh = WarderBox(db_path=db)
    fresh.load_history_from_db()
    assert abs(fresh.budget_summary["spent"] - 2.0) < 1e-6
    assert abs(CostForecaster(fresh).forecast().total_spent - 2.0) < 1e-6


# R9 — persisted quota survives a restart that re-declares the quota
def test_r9_quota_persists_through_resetquota(tmp_path):
    path = str(tmp_path / "q.json")
    qm = QuotaManager(persist_path=path)
    qm.set_quota("send", QuotaPeriod.MONTHLY, 100)
    qm.reserve("send")
    qm.reserve("send")

    # App restarts and re-declares the same quota on startup.
    reborn = QuotaManager(persist_path=path)
    reborn.set_quota("send", QuotaPeriod.MONTHLY, 100)
    assert reborn.get_usage("send")[0]["current_calls"] == 2


def test_r9_quota_concurrent_not_overrun():
    qm = QuotaManager()
    qm.set_quota("send", QuotaPeriod.DAILY, 1)
    results = []
    start = threading.Barrier(5)

    def worker():
        start.wait()
        results.append(qm.reserve("send")[0])

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for ok in results if ok) == 1  # only one reservation wins


# P2 polish regressions
def test_p2_numeric_credit_card_masked():
    from warderbox.masking import DataMasker
    m = DataMasker()
    out = m.mask({"card": 1234567812345678})
    assert out["card"] != 1234567812345678  # 16-digit int masked


def test_p2_multiline_update_with_where_not_flagged():
    from warderbox.action import Action
    from warderbox.rules import RuleEngine
    eng = RuleEngine()
    risk, _ = eng.evaluate(Action(tool_name="run", args={"sql": "UPDATE t SET x=1\nWHERE id=5"}))
    assert risk.value != "high"


def test_p2_mode_exported():
    from warderbox import Mode
    assert Mode("test").value == "test"


def test_p2_async_def_with_sync_tool_warns():
    s = WarderBox(budget=1.0)
    with pytest.warns(RuntimeWarning):
        @s.tool(cost=0.0)
        async def bad():
            return "x"


def test_p2_chaos_timeout_emits_timeout_event():
    from warderbox.chaos import ChaosEngine, FaultType
    s = WarderBox(budget=5.0)
    chaos = ChaosEngine()
    chaos.add_fault("flaky", FaultType.TIMEOUT, probability=1.0, latency_ms=1)
    s.set_chaos(chaos)
    events = []
    s.notifier.on("action_timeout", lambda e, d: events.append(d))

    @s.tool(cost=0.01)
    def flaky() -> str:
        return "ok"

    flaky()
    assert len(events) == 1
