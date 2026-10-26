"""Hardening regression tests."""

from __future__ import annotations

import time

from warderbox import (
    ApprovalManager,
    CircuitBreaker,
    FallbackManager,
    WarderBox,
)


# #1 a raising circuit-breaker callback must not corrupt a successful call
def test_i1_breaker_onclose_exception_isolated():
    def on_close(tool):
        raise RuntimeError("on_close boom")

    cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01,
                        success_threshold=1, on_close=on_close)
    cb.record_failure("t")
    time.sleep(0.02)  # -> half-open; the shield call becomes the probe

    s = WarderBox(budget=5.0)
    s.set_circuit_breaker(cb)

    @s.tool(cost=0.01)
    def t():
        return "ok"

    r = t()  # probe succeeds -> record_success -> on_close raises (isolated)
    assert r.success is True
    assert r.output == "ok"


def test_i1b_breaker_onopen_exception_isolated():
    def on_open(tool):
        raise RuntimeError("on_open boom")

    cb = CircuitBreaker(failure_threshold=1, on_open=on_open)
    s = WarderBox(budget=5.0)
    s.set_circuit_breaker(cb)

    # First call fails inside the tool; the breaker opens and on_open raises,
    # but the failure must be reported as a normal error result, not a crash.
    @s.tool(cost=0.01)
    def t():
        raise ValueError("tool failed")

    r = t()
    assert r.error is not None


# #4 a raising on_block callback fails closed (blocked), never crashes
def test_i4_on_block_exception_fails_closed():
    def on_block(action, reasons):
        raise RuntimeError("on_block boom")

    s = WarderBox(budget=5.0, on_block=on_block)

    @s.tool(cost=0.01)
    def bulk(count: int):
        return "ran"

    r = bulk(count=500)  # HIGH risk -> consults on_block
    assert r.blocked is True


# #4b a raising approval request callback does not crash the flow
def test_i4b_approval_request_callback_isolated():
    def on_req(**kwargs):
        raise RuntimeError("notify boom")

    ap = ApprovalManager(default_timeout=0.05, on_approval_request=on_req)
    ap.require_approval("act", auto_deny=True)
    s = WarderBox(budget=5.0)
    s.set_approval(ap)

    @s.tool(cost=0.01)
    def act():
        return "ok"

    r = act()
    assert r.blocked is True  # timed out / denied, but no crash


# #2 test-mode fallback simulates budget (reports a would-be budget block)
def test_i2_test_mode_fallback_simulates_budget():
    s = WarderBox(budget=0.05, mode="test")
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=1.0)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    r = primary()
    assert r.blocked is True  # fallback cost 1.0 > budget 0.05 even in dry-run


# #3 fallback execution produces a tracer span
def test_i3_fallback_creates_tracer_span():
    from warderbox import Tracer
    tr = Tracer()
    s = WarderBox(budget=5.0)
    s.set_tracer(tr)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()
    fb.register("primary", "secondary", fallback_fn=lambda **k: "fb", fallback_cost=0.01)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    primary()
    names = [sp.name for sp in tr.spans()]
    assert any("secondary" in n for n in names)
