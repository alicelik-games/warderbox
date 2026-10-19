"""Tests for pre-execution cost & duration estimation."""

import time
from warderbox import WarderBox, CostEstimator, Estimate


class TestCostEstimator:
    def test_estimate_with_declared_costs_only(self):
        est = CostEstimator()
        est.set_tool_cost("gpt4", 0.03)
        est.set_tool_cost("search", 0.01)

        result = est.estimate(["gpt4", "search", "gpt4"])
        assert result.expected_cost == 0.07
        assert result.confidence == 0.0
        assert result.data_points == 0

    def test_estimate_improves_with_data(self):
        est = CostEstimator()
        est.set_tool_cost("gpt4", 0.03)

        e1 = est.estimate(["gpt4"])
        assert e1.confidence == 0.0

        for _ in range(5):
            est.record("gpt4", cost=0.028, duration=1.0, success=True)

        e2 = est.estimate(["gpt4"])
        assert e2.confidence > e1.confidence
        assert e2.data_points == 5

    def test_confidence_increases_with_data(self):
        est = CostEstimator()
        est.set_tool_cost("tool", 0.01)

        for i in range(100):
            est.record("tool", cost=0.01, duration=0.5, success=True)

        e = est.estimate(["tool"])
        assert e.confidence == 0.95

    def test_failure_rate_increases_expected_cost(self):
        est = CostEstimator()
        for i in range(10):
            est.record("flaky", cost=0.02, duration=1.0, success=(i % 2 == 0))

        e = est.estimate(["flaky"])
        assert e.expected_cost > e.best_cost

    def test_worst_case_higher_than_expected(self):
        est = CostEstimator()
        est.record("tool", cost=0.01, duration=0.5, success=True)
        est.record("tool", cost=0.05, duration=2.0, success=True)
        est.record("tool", cost=0.02, duration=1.0, success=False)

        e = est.estimate(["tool"])
        assert e.worst_cost >= e.expected_cost >= e.best_cost
        assert e.worst_duration >= e.expected_duration >= e.best_duration

    def test_empty_plan(self):
        est = CostEstimator()
        e = est.estimate([])
        assert e.expected_cost == 0.0
        assert e.confidence == 0.0

    def test_unknown_tool_uses_zero(self):
        est = CostEstimator()
        e = est.estimate(["unknown_tool"])
        assert e.expected_cost == 0.0
        assert e.confidence == 0.0

    def test_summary_format(self):
        est = CostEstimator()
        est.set_tool_cost("tool", 0.01)
        est.record("tool", cost=0.01, duration=0.5, success=True)

        e = est.estimate(["tool"])
        s = e.summary()
        assert "cost" in s
        assert "best" in s["cost"]
        assert "expected" in s["cost"]
        assert "worst" in s["cost"]
        assert "duration_seconds" in s
        assert "confidence" in s
        assert "data_points" in s
        assert "tools" in s

    def test_repr(self):
        est = CostEstimator()
        est.set_tool_cost("tool", 0.01)
        e = est.estimate(["tool"])
        r = repr(e)
        assert "Estimate(" in r
        assert "cost=" in r

    def test_multi_tool_plan(self):
        est = CostEstimator()
        for _ in range(20):
            est.record("gpt4", cost=0.03, duration=1.5, success=True)
            est.record("search", cost=0.01, duration=0.3, success=True)

        e = est.estimate(["gpt4", "search", "gpt4", "search"])
        assert e.expected_cost > 0.07
        assert e.data_points == 80
        assert "gpt4" in e.tool_estimates
        assert "search" in e.tool_estimates

    def test_get_tool_stats(self):
        est = CostEstimator()
        est.record("tool_a", cost=0.02, duration=1.0, success=True)
        est.record("tool_a", cost=0.03, duration=1.5, success=False)

        stats = est.get_tool_stats("tool_a")
        assert stats is not None
        assert stats["total_calls"] == 2
        assert stats["successes"] == 1
        assert stats["failures"] == 1
        assert stats["failure_rate"] == 0.5

    def test_get_tool_stats_unknown(self):
        est = CostEstimator()
        assert est.get_tool_stats("nope") is None

    def test_global_stats(self):
        est = CostEstimator()
        est.record("a", cost=0.01, duration=0.5, success=True)
        est.record("b", cost=0.02, duration=1.0, success=True)

        s = est.stats()
        assert s["tracked_tools"] == 2
        assert s["total_data_points"] == 2

    def test_reset_specific_tool(self):
        est = CostEstimator()
        est.record("a", cost=0.01, duration=0.5, success=True)
        est.record("b", cost=0.02, duration=1.0, success=True)
        est.reset("a")

        assert est.get_tool_stats("a") is None
        assert est.get_tool_stats("b") is not None

    def test_reset_all(self):
        est = CostEstimator()
        est.record("a", cost=0.01, duration=0.5, success=True)
        est.record("b", cost=0.02, duration=1.0, success=True)
        est.reset()
        assert est.stats()["tracked_tools"] == 0

    def test_max_history_per_tool(self):
        est = CostEstimator(max_history_per_tool=5)
        for i in range(10):
            est.record("tool", cost=0.01 * (i + 1), duration=0.1, success=True)

        stats = est.get_tool_stats("tool")
        assert stats["total_calls"] == 5

    def test_declared_duration(self):
        est = CostEstimator()
        est.set_tool_cost("slow_api", 0.05)
        est.set_tool_duration("slow_api", 10.0)

        e = est.estimate(["slow_api"])
        assert e.expected_duration == 10.0
        assert e.best_duration == 5.0


class TestShieldEstimatorIntegration:
    def test_shield_has_estimator(self):
        shield = WarderBox()
        assert isinstance(shield.estimator, CostEstimator)

    def test_shield_auto_records(self):
        shield = WarderBox()

        @shield.tool(cost=0.02)
        def my_tool(x: int) -> int:
            return x * 2

        my_tool(5)
        my_tool(10)

        stats = shield.estimator.get_tool_stats("my_tool")
        assert stats is not None
        assert stats["total_calls"] == 2
        assert stats["successes"] == 2

    def test_shield_estimate_method(self):
        shield = WarderBox()

        @shield.tool(cost=0.03)
        def gpt4(q: str) -> str:
            return "answer"

        @shield.tool(cost=0.01)
        def search(q: str) -> str:
            return "result"

        for _ in range(10):
            gpt4(q="test")
            search(q="test")

        result = shield.estimate(["gpt4", "search", "gpt4"])
        assert "cost" in result
        assert result["cost"]["expected"] > 0
        assert result["confidence"] > 0

    def test_shield_records_failures(self):
        shield = WarderBox()

        @shield.tool(cost=0.01)
        def failing_tool(x: int) -> int:
            raise ValueError("boom")

        failing_tool(1)
        failing_tool(2)

        stats = shield.estimator.get_tool_stats("failing_tool")
        assert stats["failures"] == 2
        assert stats["successes"] == 0

    def test_self_improving_accuracy(self):
        shield = WarderBox()

        @shield.tool(cost=0.05)
        def api_call(x: int) -> int:
            return x

        e1 = shield.estimate(["api_call"])
        assert e1["confidence"] == 0

        for i in range(25):
            api_call(i)

        e2 = shield.estimate(["api_call"])
        assert e2["confidence"] > 0
        assert e2["data_points"] == 25
