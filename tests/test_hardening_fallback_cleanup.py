"""Hardening regression tests.

Covers: fallback error-path cleanup (validator / BaseException), fallback
history+snapshot+reversibility, interruption audit/span, idempotency backend
isolation, and test-mode fallback rate/quota simulation.
"""

from __future__ import annotations

import asyncio

import pytest

from warderbox import (
    AsyncWarderBox,
    CircuitBreaker,
    FallbackManager,
    IdempotencyGuard,
    OutputRule,
    OutputValidator,
    QuotaManager,
    QuotaPeriod,
    Tracer,
    WarderBox,
)
from warderbox.idempotency import DedupeResult, MemoryBackend


def _fallback_setup(**kwargs):
    s = WarderBox(budget=5.0, **kwargs)
    cb = CircuitBreaker(failure_threshold=1)
    s.set_circuit_breaker(cb)
    fb = FallbackManager()
    return s, cb, fb


def _wire_primary(s, cb, fb, fallback_fn, cost=0.01):
    fb.register("primary", "secondary", fallback_fn=fallback_fn,
                fallback_cost=cost)
    s.set_fallback(fb)

    @s.tool(cost=0.01)
    def primary():
        return "p"

    cb.record_failure("primary")
    return primary


def test_k1_fallback_validator_exception_is_clean_error():
    validator = OutputValidator()
    validator.add_rule(OutputRule(
        name="boom",
        validator=lambda _out: (_ for _ in ()).throw(RuntimeError("boom")),
    ))
    s, cb, fb = _fallback_setup()
    s.set_output_validator(validator)
    guard = IdempotencyGuard()
    guard.add_protected_tool("secondary")
    s.set_idempotency(guard)
    tracer = Tracer()
    s.set_tracer(tracer)
    primary = _wire_primary(s, cb, fb, lambda **k: "fb")

    r = primary()  # must not raise
    assert r.success is False
    assert "Output validation failed" in (r.error or "")
    assert r.fallback_tool == "secondary"
    assert s.budget_summary["reserved"] == 0.0
    assert s.budget_summary["active_reservations"] == 0
    assert abs(s.budget_summary["spent"] - 0.01) < 1e-9
    assert guard.check("secondary", {})[0] == DedupeResult.DUPLICATE
    assert s.history()[-1]["fallback"] == "secondary"
    assert tracer.spans()[-1].end_time > 0


def test_k2_fallback_baseexception_releases_resources():
    s, cb, fb = _fallback_setup()
    primary = _wire_primary(
        s, cb, fb, lambda **k: (_ for _ in ()).throw(KeyboardInterrupt("stop")),
    )

    with pytest.raises(KeyboardInterrupt):
        primary()

    assert s.budget_summary["reserved"] == 0.0
    assert s.budget_summary["active_reservations"] == 0
    assert any(
        "Fallback interrupted" in (h["error"] or "")
        for h in s.history()
    )


def test_k3_fallback_history_snapshot_reversibility():
    s, cb, fb = _fallback_setup()
    state = {"value": 1}

    def secondary() -> str:
        state["value"] = 2
        return "fb"

    # Register snapshot/restore for the fallback tool name.
    s.tool(
        cost=0.01,
        snapshot_state=lambda: dict(state),
        restore_state=lambda snap: state.update(snap),
    )(secondary)
    primary = _wire_primary(s, cb, fb, secondary)

    r = primary()
    assert r.success is True
    hist = s.history()[-1]
    assert hist["fallback"] == "secondary"
    assert hist["has_snapshot"] is True
    assert hist["reversible"] is True
    assert s.rollback() is True
    assert state["value"] == 1


def test_k4_sync_interruption_records_history_and_closes_span():
    s = WarderBox(budget=5.0)
    tracer = Tracer()
    s.set_tracer(tracer)

    @s.tool(cost=1.0)
    def interrupted() -> str:
        raise KeyboardInterrupt("stop")

    with pytest.raises(KeyboardInterrupt):
        interrupted()

    assert s.budget_summary["reserved"] == 0.0
    assert s.budget_summary["spent"] == 0.0
    hist = s.history()
    assert hist and "Interrupted" in (hist[-1]["error"] or "")
    spans = tracer.spans()
    assert spans and spans[-1].end_time > 0


def test_k5_async_cancellation_records_history_and_closes_span():
    s = AsyncWarderBox(budget=5.0)
    tracer = Tracer()
    s.set_tracer(tracer)

    @s.async_tool(cost=1.0)
    async def slow() -> str:
        await asyncio.sleep(1.0)
        return "ok"

    async def main():
        task = asyncio.create_task(slow())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert s.budget_summary["reserved"] == 0.0
    hist = s.history()
    assert hist and "Cancelled" in (hist[-1]["error"] or "")
    spans = tracer.spans()
    assert spans and spans[-1].end_time > 0


def test_k6_idempotency_backend_complete_failure_isolated():
    class BadBackend(MemoryBackend):
        def set(self, key, record):
            raise RuntimeError("backend set boom")

    guard = IdempotencyGuard(backend=BadBackend())
    guard.add_protected_tool("charge")
    s = WarderBox(budget=5.0)
    s.set_idempotency(guard)
    events = []
    s.notifier.on("idempotency_error", lambda event, data: events.append(event))

    @s.tool(cost=0.01)
    def charge() -> str:
        return "ok"

    r = charge()
    assert r.success is True
    assert events == ["idempotency_error"]


def test_k7_test_mode_fallback_simulates_rate_and_quota():
    # Rate limit exhausted -> dry-run reports the block.
    s, cb, fb = _fallback_setup(mode="test", global_rpm=0)
    primary = _wire_primary(s, cb, fb, lambda **k: "fb")
    assert primary().blocked is True

    # Quota exhausted -> dry-run reports the block.
    s2, cb2, fb2 = _fallback_setup(mode="test")
    qm = QuotaManager()
    qm.set_quota("secondary", QuotaPeriod.DAILY, 0)
    s2.set_quota(qm)
    primary2 = _wire_primary(s2, cb2, fb2, lambda **k: "fb")
    assert primary2().blocked is True


def test_k8_fallback_attributed_to_actual_tool(tmp_path):
    from warderbox.compliance import ComplianceReporter
    from warderbox.forecast import CostForecaster
    from warderbox.logger import ActionLogger

    db = str(tmp_path / "audit.db")
    s, cb, fb = _fallback_setup(db_path=db)
    primary = _wire_primary(s, cb, fb, lambda **k: "fb", cost=0.02)
    primary()
    s._logger.close()

    logger = ActionLogger(db)
    stats = logger.stats()
    logger.close()
    assert "secondary" in stats["by_tool"]
    assert "primary" not in stats["by_tool"]

    report = ComplianceReporter(db).generate_report()
    assert "secondary" in report.tool_usage
    assert "primary" not in report.tool_usage

    forecast = CostForecaster(s).forecast()
    assert "secondary" in forecast.cost_by_tool
    assert "primary" not in forecast.cost_by_tool


def test_k9_reversible_roundtrips_through_db(tmp_path):
    db = str(tmp_path / "rev.db")
    s = WarderBox(budget=5.0, db_path=db)
    state = {"value": 1}

    @s.tool(
        cost=0.01,
        snapshot_state=lambda: dict(state),
        restore_state=lambda snap: state.update(snap),
    )
    def act() -> str:
        return "ok"

    act()
    assert s.history()[-1]["reversible"] is True
    s._logger.close()

    fresh = WarderBox(db_path=db)
    fresh.load_history_from_db()
    assert fresh.history()[-1]["reversible"] is True
