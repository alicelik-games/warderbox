"""Hardening regression tests."""

from __future__ import annotations

import asyncio
import threading
import time

from warderbox import (
    ApprovalManager,
    AsyncWarderBox,
    CircuitBreaker,
    FallbackManager,
    IdempotencyGuard,
    WarderBox,
)
from warderbox.ratelimit import ToolLimit


def _fb_shield(**kw):
    s = WarderBox(budget=5.0, **kw)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    return s, cb, FallbackManager()


# #1 approval policy on the fallback tool only
def test_f1_fallback_only_approval_gated():
    ap = ApprovalManager(default_timeout=0.1)
    ap.require_approval("secondary", auto_deny=True)
    s, cb, fb = _fb_shield()
    s.set_approval(ap)
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    assert primary().blocked is True


# #2 protected fallback tool dedupes
def test_f2_fallback_protected_idempotent():
    guard = IdempotencyGuard()
    guard.add_protected_tool("secondary")
    s, cb, fb = _fb_shield()
    s.set_idempotency(guard)
    runs = []
    fb.register("primary", "secondary",
                fallback_fn=lambda **k: (runs.append(1) or "fb"), fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    primary()
    primary()
    assert len(runs) == 1


# #3 on_block consulted for HIGH-risk fallback
def test_f3_fallback_onblock_consulted():
    seen = []

    def on_block(action, reasons):
        seen.append(action.tool_name)
        return False

    s, cb, fb = _fb_shield(on_block=on_block)
    fb.register("del_many", "safe_delete", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def del_many(count: int):
        return "p"

    cb.record_failure("del_many")
    r = del_many(count=500)  # HIGH (bulk > 100)
    assert r.blocked is True
    assert len(seen) == 1


# #4 breaker callback may re-enter without deadlocking
def test_f4_breaker_callback_no_deadlock():
    done = {"ok": False}

    def on_open(tool):
        cb.get_state(tool)   # re-enters the breaker
        done["ok"] = True

    cb = CircuitBreaker(failure_threshold=1, on_open=on_open)
    t = threading.Thread(target=lambda: cb.record_failure("x"), daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert done["ok"] is True
    assert not t.is_alive()


# #5 rate limit concurrency
def test_f5_rate_limit_no_overrun():
    s = WarderBox(budget=100.0, tool_limits={"hit": ToolLimit(rpm=1)})

    @s.tool(cost=0.0)
    def hit():
        time.sleep(0.02)
        return "ok"

    start = threading.Barrier(5)
    res = []

    def w():
        start.wait()
        res.append(hit())

    ts = [threading.Thread(target=w) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for r in res if r.success) == 1


# #6 cancelled async task releases its budget reservation
def test_f6_async_cancel_releases_budget():
    s = AsyncWarderBox(budget=1.0)

    @s.async_tool(cost=0.4)
    async def slow():
        await asyncio.sleep(1.0)
        return "ok"

    async def main():
        task = asyncio.create_task(slow())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return s.budget_summary["reserved"]

    reserved_after = asyncio.run(main())
    assert reserved_after == 0.0
