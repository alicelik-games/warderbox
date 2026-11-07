"""Primary and fallback calls share one pipeline; custom guards plug into it."""

from __future__ import annotations

import pytest

from warderbox import (
    AsyncWarderBox,
    Block,
    CapabilityManager,
    CircuitBreaker,
    FallbackManager,
    Guard,
    IdempotencyGuard,
    OutputRule,
    OutputValidator,
    QuotaManager,
    QuotaPeriod,
    RiskLevel,
    ToolLimit,
    WarderBox,
)
from warderbox.action import Action


def _with_fallback(fallback, cost=0.25, **kwargs):
    shield = WarderBox(budget=kwargs.pop("budget", 5.0), **kwargs)
    breaker = CircuitBreaker(failure_threshold=1)
    shield.set_circuit_breaker(breaker)
    manager = FallbackManager()
    manager.register("primary", "secondary", fallback_fn=fallback, fallback_cost=cost)
    shield.set_fallback(manager)

    @shield.tool(cost=0.01)
    def primary(**kw):
        return "primary"

    breaker.record_failure("primary")
    return shield, primary


def _nothing_held(shield):
    summary = shield.budget_summary
    assert summary["active_reservations"] == 0
    assert summary["reserved"] == 0


def test_output_block_on_fallback_returns_result_and_charges():
    # Reading `result` before assignment used to crash this exact path.
    validator = OutputValidator()
    validator.add_rule(OutputRule(name="no", validator=lambda out: False))
    shield, primary = _with_fallback(lambda **k: "secret")
    shield.set_output_validator(validator)
    events = []
    shield.notifier.on("output_blocked", lambda e, d: events.append(d))
    result = primary()
    assert "Output blocked" in result.error
    assert result.output_withheld
    assert result.fallback_tool == "secondary"
    assert result.output is None
    assert shield.budget_summary["spent"] == 0.25
    assert events
    _nothing_held(shield)


@pytest.mark.parametrize("configure, reason", [
    (lambda s: _deny_capability(s), "Capability denied (fallback)"),
    (lambda s: s._rate_limiter.__init__(
        tool_limits={"secondary": ToolLimit(rpm=0)}
    ), "rate limit"),
    (lambda s: _zero_quota(s), "Quota exceeded"),
])
def test_every_gate_applies_to_fallback(configure, reason):
    ran = []
    shield, primary = _with_fallback(lambda **k: ran.append(1))
    configure(shield)
    result = primary()
    assert result.blocked and reason in result.block_reason
    assert result.fallback_tool == "secondary"
    assert shield.history()[-1]["fallback"] == "secondary"
    assert not ran
    _nothing_held(shield)


def _deny_capability(shield):
    caps = CapabilityManager()
    caps.grant("agent", allow=["primary"])
    shield._capabilities = caps
    shield._agent_name = "agent"


def _zero_quota(shield):
    quota = QuotaManager()
    quota.set_quota("secondary", QuotaPeriod.DAILY, 0)
    shield.set_quota(quota)


def test_fallback_budget_gate():
    ran = []
    shield, primary = _with_fallback(lambda **k: ran.append(1), cost=10.0)
    events = []
    shield.notifier.on("budget_exceeded", lambda e, d: events.append(d))
    result = primary()
    assert result.blocked and "Fallback budget exceeded" in result.block_reason
    assert not ran and len(events) == 1
    _nothing_held(shield)


def test_fallback_rules_see_the_tool_that_will_run():
    ran = []
    shield = WarderBox(budget=5.0)
    breaker = CircuitBreaker(failure_threshold=1)
    shield.set_circuit_breaker(breaker)
    manager = FallbackManager()
    manager.register(
        "primary", "delete_file", fallback_fn=lambda: ran.append(1), fallback_cost=0.0
    )
    shield.set_fallback(manager)
    shield._on_block = lambda action, reasons: False

    @shield.tool(cost=0.01)
    def primary():
        return "p"

    breaker.record_failure("primary")
    result = primary()
    assert result.blocked and "fallback" in result.block_reason
    assert not ran


def test_blocked_later_gate_releases_earlier_reservations():
    shield = WarderBox(budget=5.0, tool_limits={"tool": ToolLimit(rpm=5)})
    quota = QuotaManager()
    quota.set_quota("tool", QuotaPeriod.DAILY, 0)
    shield.set_quota(quota)

    @shield.tool(cost=1.0)
    def tool():
        return "x"

    assert tool().blocked
    _nothing_held(shield)
    assert shield._rate_limiter.stats()["by_tool"].get("tool", {}).get("last_minute", 0) == 0


class _Recording(Guard):
    name = "recording"
    failure_label = "Recording failed"

    def __init__(self, log):
        self.log = log

    def reserve(self, ctx):
        self.log.append(("reserve", ctx.tool))

    def release(self, ctx):
        self.log.append(("release", ctx.tool))

    def commit(self, ctx, result):
        self.log.append(("commit", ctx.tool, result.success))


def test_custom_guard_lifecycle_on_success_and_on_block():
    log = []
    shield = WarderBox(budget=1.0)
    shield.add_guard(_Recording(log))

    @shield.tool(cost=0.6)
    def tool():
        return "x"

    assert tool().success
    assert log == [("reserve", "tool"), ("commit", "tool", True)]

    log.clear()
    assert tool().blocked  # budget is the next gate after the custom guard
    assert log == [("reserve", "tool"), ("release", "tool")]


def test_custom_guard_can_block_and_runs_before_budget():
    class Closed(Guard):
        name = "closed"

        def check(self, ctx):
            return Block(f"{ctx.tool} is closed")

    shield = WarderBox(budget=1.0)
    shield.add_guard(Closed())
    ran = []

    @shield.tool(cost=0.5)
    def tool():
        ran.append(1)

    result = tool()
    assert result.blocked and result.block_reason == "tool is closed"
    assert not ran
    assert shield.history()[-1]["blocked"]
    _nothing_held(shield)


def test_custom_guard_applies_to_fallback_too():
    log = []
    shield, primary = _with_fallback(lambda **k: "fb")
    shield.add_guard(_Recording(log))
    assert primary().output == "fb"
    assert ("reserve", "secondary") in log


def test_failing_guard_stops_the_call():
    class Broken(Guard):
        name = "broken"

        def check(self, ctx):
            raise RuntimeError("guard down")

    shield = WarderBox(budget=1.0)
    shield.add_guard(Broken())
    ran = []

    @shield.tool(cost=0.1)
    def tool():
        ran.append(1)

    result = tool()
    assert result.blocked and "guard down" in result.block_reason
    assert not ran


def test_failing_release_does_not_stop_other_releases():
    class BadRelease(Guard):
        name = "bad_release"

        def release(self, ctx):
            raise RuntimeError("no")

    shield = WarderBox(budget=1.0)
    shield.add_guard(BadRelease(), before="quota")
    errors = []
    shield.notifier.on("guard_error", lambda e, d: errors.append(d))
    quota = QuotaManager()
    quota.set_quota("tool", QuotaPeriod.DAILY, 0)
    shield.set_quota(quota)

    @shield.tool(cost=0.5)
    def tool():
        return "x"

    assert tool().blocked
    assert errors[0]["guard"] == "bad_release"
    _nothing_held(shield)


def test_add_guard_rejects_unknown_position():
    with pytest.raises(ValueError):
        WarderBox().add_guard(Guard(), before="nope")


def test_reused_action_id_does_not_share_a_reservation():
    shield = WarderBox(budget=1.0)
    first = shield._budget.reserve("same", "t", 0.4)
    second = shield._budget.reserve("same", "t", 0.4)
    assert first.reservation_id != second.reservation_id
    shield._budget.commit(first.reservation_id)
    shield._budget.commit(second.reservation_id)
    assert shield.budget_summary["spent"] == 0.8


def test_duplicate_protected_call_holds_no_snapshot():
    state = {"v": 0}
    shield = WarderBox(budget=1.0)
    guard = IdempotencyGuard()
    guard.add_protected_tool("pay")
    shield.set_idempotency(guard)

    @shield.tool(cost=0.1, snapshot_state=lambda: dict(state),
                 restore_state=lambda s: state.update(s))
    def pay():
        state["v"] += 1
        return "ok"

    pay()
    second = pay()
    assert second.blocked and second.output == "ok"
    assert state["v"] == 1
    assert shield._snapshots.count == 1


def test_public_execute_runs_the_same_pipeline():
    shield = WarderBox(budget=1.0)
    result = shield.execute(
        Action(tool_name="run_sql", args={"sql": "DROP TABLE x"}), lambda sql: "ran",
        kwargs={"sql": "DROP TABLE x"}, risk_override=RiskLevel.LOW,
    )
    assert result.blocked


def test_async_shield_shares_the_pipeline():
    import asyncio

    log = []
    shield = AsyncWarderBox(budget=1.0)
    shield.add_guard(_Recording(log))

    @shield.async_tool(cost=0.1)
    async def tool():
        return "x"

    assert asyncio.run(tool()).output == "x"
    assert log == [("reserve", "tool"), ("commit", "tool", True)]
