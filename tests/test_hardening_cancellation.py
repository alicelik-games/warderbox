"""Hardening regression tests."""

from __future__ import annotations

import asyncio
import threading
import time

from warderbox import (
    AsyncWarderBox,
    CapabilityManager,
    CircuitBreaker,
    FallbackManager,
    IdempotencyGuard,
    WarderBox,
)


# #1 cancel AFTER the side effect must NOT allow a duplicate retry
def test_h1_async_cancel_after_sideeffect_no_duplicate():
    guard = IdempotencyGuard()
    guard.add_protected_tool("charge")
    a = AsyncWarderBox(budget=50.0)
    a.set_idempotency(guard)
    sent = []

    @a.async_tool(cost=0.01)
    async def charge(x: int):
        sent.append(x)              # side effect first
        await asyncio.sleep(0.5)    # cancel lands here, after the effect
        return "ok"

    async def main():
        t = asyncio.create_task(charge(x=1))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return await charge(x=1)     # retry

    r = asyncio.run(main())
    assert len(sent) == 1
    assert r.blocked is True


# #1b cancel BEFORE execution starts should allow a retry (marker released)
def test_h1b_async_cancel_before_execution_allows_retry():
    guard = IdempotencyGuard()
    guard.add_protected_tool("slow")
    ap_free = AsyncWarderBox(budget=5.0)
    ap_free.set_idempotency(guard)
    ran = []

    @ap_free.async_tool(cost=0.01)
    async def slow(x: int):
        ran.append(x)
        return "ok"

    # Pre-reserve the marker as if a prior call began but was cancelled pre-exec.
    guard.begin("slow", {"x": 1})
    guard.fail("slow", {"x": 1}, keep=False)  # released

    r = asyncio.run(slow(x=1))
    assert r.blocked is False
    assert ran == [1]


# #3 test mode must still evaluate fallback policies (capability)
def test_h3_test_mode_fallback_policy_enforced():
    caps = CapabilityManager()
    caps.grant("bot", allow=["primary"])   # secondary denied
    s = WarderBox(budget=5.0, mode="test", agent_name="bot")
    s.set_capabilities(caps)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    assert primary().blocked is True


# #4 breaker callback must not be invoked while holding the lock
def test_h4_breaker_callback_not_holding_lock():
    def slow_on_open(tool):
        time.sleep(0.4)

    cb = CircuitBreaker(failure_threshold=1, on_open=slow_on_open)
    t = threading.Thread(target=lambda: cb.record_failure("x"))
    t.start()
    time.sleep(0.05)
    t0 = time.time()
    cb.should_allow("y")   # unrelated tool
    dur = time.time() - t0
    t.join()
    assert dur < 0.2       # not stalled by the running callback


# #5 fallback execution is recorded in the estimator
def test_h5_fallback_recorded_in_estimator():
    s = WarderBox(budget=5.0)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.02)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    primary()
    assert s.estimator.stats()["total_data_points"] >= 1


# #6 load_history must not wipe active reservations
def test_h6_load_history_keeps_active_reservations(tmp_path):
    db = str(tmp_path / "a.db")
    s = WarderBox(budget=10.0, db_path=db)

    @s.tool(cost=1.0)
    def act():
        return "ok"

    act()
    s._logger.close()

    fresh = WarderBox(db_path=db)
    res = fresh._budget.reserve("live", "tool", 1.0)
    fresh.load_history_from_db()
    # The live reservation survives and can still be committed.
    fresh._budget.commit(res.reservation_id)
