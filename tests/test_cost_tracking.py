"""Real charges reach the budget, the history, the forecast and the alerts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from warderbox import (
    AnomalyDetector,
    CircuitBreaker,
    CostForecaster,
    FallbackManager,
    WarderBox,
    cost_from_usage,
)


def test_cost_source_is_reported():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=1.0)
    def declared():
        return "x"

    @shield.tool(cost=1.0, actual_cost=lambda out: 0.25)
    def measured():
        return "x"

    assert declared().cost_source == "declared"
    result = measured()
    assert (result.cost_source, result.cost) == ("actual", 0.25)
    assert [h["cost_source"] for h in shield.history()] == ["declared", "actual"]


def test_broken_cost_callback_keeps_the_declared_cost():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=1.0, actual_cost=lambda out: 1 / 0)
    def tool():
        return "x"

    result = tool()
    assert result.cost == 1.0 and result.cost_source == "declared"


def test_fallback_uses_its_own_actual_cost():
    shield = WarderBox(budget=5.0)
    breaker = CircuitBreaker(failure_threshold=1)
    shield.set_circuit_breaker(breaker)
    manager = FallbackManager()
    manager.register("primary", "secondary", fallback_fn=lambda: "fb", fallback_cost=1.0)
    shield.set_fallback(manager)
    shield.set_actual_cost("secondary", lambda out: 0.3)

    @shield.tool(cost=0.01)
    def primary():
        return "p"

    breaker.record_failure("primary")
    result = primary()
    assert (result.cost, result.cost_source) == (0.3, "actual")
    assert shield.budget_summary["spent"] == 0.3


def test_set_actual_cost_works_for_adapter_tools():
    from warderbox.integrations.langchain import ShieldedTool

    shield = WarderBox(budget=5.0)
    shield.set_actual_cost("search", lambda out: 0.02)
    tool = ShieldedTool(shield=shield, name="search", func=lambda q: "r", cost=1.0)
    tool.run("hello")
    assert shield.budget_summary["spent"] == 0.02


@pytest.mark.parametrize("output, expected", [
    ({"usage": {"input_tokens": 1000, "output_tokens": 100}}, 0.0045),
    ({"usage": {"prompt_tokens": 1000, "completion_tokens": 100}}, 0.0045),
    (SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=100)), 0.0045),
])
def test_cost_from_usage_reads_provider_shapes(output, expected):
    assert cost_from_usage(3.0, 15.0)(output) == pytest.approx(expected)


def test_cost_from_usage_prices_cached_tokens_when_asked():
    usage = {"usage": {"input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 1_000_000}}
    assert cost_from_usage(3.0, 15.0, cached_input_per_mtok=0.3)(usage) == 0.3


def test_cost_from_usage_without_a_report_falls_back_to_declared():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=1.0, actual_cost=cost_from_usage(3.0, 15.0))
    def tool():
        return "no usage here"

    result = tool()
    assert result.cost == 1.0 and result.cost_source == "declared"


def test_reconcile_updates_budget_history_and_estimator():
    shield = WarderBox(budget=5.0)
    events = []
    shield.notifier.on("cost_reconciled", lambda e, d: events.append(d))

    @shield.tool(cost=1.0)
    def tool():
        return "x"

    result = tool()
    assert shield.reconcile_cost(result.action.id, 3.0)
    assert shield.budget_summary["spent"] == 3.0
    entry = shield.history()[-1]
    assert (entry["cost"], entry["cost_source"]) == (3.0, "reconciled")
    assert shield.estimator._stats["tool"].costs[-1] == 3.0
    assert events[0]["recorded"] == 1.0 and events[0]["actual"] == 3.0


def test_reconcile_unknown_action_is_false():
    assert not WarderBox(budget=1.0).reconcile_cost("missing", 1.0)


def test_reconcile_can_reveal_an_anomaly_the_estimate_hid():
    shield = WarderBox(budget=1000.0)
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=1000)
    shield.set_anomaly_detector(detector)
    alerts = []
    shield.notifier.on("anomaly_detected", lambda e, d: alerts.append(d))

    @shield.tool(cost=1.0)
    def tool(i):
        return i

    results = [tool(i) for i in range(40)]
    assert not alerts
    shield.reconcile_cost(results[-1].action.id, 500.0)
    assert alerts


def test_forecast_reports_how_much_spend_is_real():
    shield = WarderBox(budget=100.0)

    @shield.tool(cost=1.0)
    def declared():
        return "x"

    @shield.tool(cost=1.0, actual_cost=lambda out: 3.0)
    def measured():
        return "x"

    declared()
    measured()
    forecast = CostForecaster(shield, min_window_seconds=0.0).forecast()
    assert forecast.actual_cost_share == 0.75
    assert forecast.to_dict()["actual_cost_share"] == 0.75
