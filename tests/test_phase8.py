"""Tests for Phase 8: tracing, chaos engineering, cost forecasting, agent scoring."""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, "src")

from warderbox import (
    AgentScorer,
    ChaosEngine,
    CostForecaster,
    FaultType,
    PoolScorer,
    Tracer,
    WarderBox,
)
from warderbox.multiagent import AgentPool

# -- Distributed Tracing --

def test_tracer_basic_span():
    tracer = Tracer()
    span = tracer.start_span("test_op", attributes={"key": "value"})
    assert span.trace_id
    assert span.span_id
    tracer.end_span(span, "ok")
    assert span.duration_ms > 0
    assert span.status == "ok"


def test_tracer_context_manager():
    tracer = Tracer()
    with tracer.span("parent") as parent:
        parent.set_attribute("x", 1)
        with tracer.span("child", parent=parent) as child:
            child.add_event("something_happened")

    spans = tracer.spans()
    assert len(spans) == 2
    assert spans[1].parent_span_id == spans[0].span_id
    assert spans[0].trace_id == spans[1].trace_id


def test_tracer_error_span():
    tracer = Tracer()
    try:
        with tracer.span("failing"):
            raise ValueError("boom")
    except ValueError:
        pass

    spans = tracer.spans()
    assert len(spans) == 1
    assert spans[0].status == "error"
    assert len(spans[0].events) == 1
    assert spans[0].events[0]["name"] == "exception"


def test_tracer_record_action():
    tracer = Tracer()
    span = tracer.record_action(
        tool_name="search",
        args={"q": "test"},
        result_status="ok",
        cost=0.05,
        risk="LOW",
    )
    assert span.name == "tool.search"
    assert span.attributes["tool.cost"] == 0.05


def test_tracer_export_json():
    tracer = Tracer(service_name="test-service")
    with tracer.span("op1"):
        pass
    with tracer.span("op2"):
        pass

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name

    try:
        tracer.export_json(path)
        with open(path) as f:
            data = json.load(f)
        assert "resourceSpans" in data
        spans = data["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2
    finally:
        os.unlink(path)


def test_tracer_otlp_export():
    tracer = Tracer()
    with tracer.span("x"):
        pass
    otlp = tracer.export_otlp()
    assert "resourceSpans" in otlp
    span_data = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert "traceId" in span_data
    assert "startTimeUnixNano" in span_data


def test_tracer_in_shield():
    tracer = Tracer()
    shield = WarderBox(budget=10.0)
    shield.set_tracer(tracer)

    @shield.tool(cost=0.01)
    def search(q: str):
        return f"results for {q}"

    search(q="hello")
    search(q="world")

    spans = tracer.spans()
    assert len(spans) == 2
    assert all(s.status == "ok" for s in spans)
    assert all(s.attributes["tool.name"] == "search" for s in spans)


def test_tracer_stats():
    tracer = Tracer()
    with tracer.span("a"):
        pass
    try:
        with tracer.span("b"):
            raise RuntimeError("x")
    except RuntimeError:
        pass

    s = tracer.stats()
    assert s["total_spans"] == 2
    assert s["errors"] == 1


def test_tracer_traces_grouping():
    tracer = Tracer()
    tid1 = tracer.start_trace()
    with tracer.span("a"):
        pass
    tracer.end_trace()

    tid2 = tracer.start_trace()
    with tracer.span("b"):
        pass
    tracer.end_trace()

    traces = tracer.traces()
    assert len(traces) == 2


# -- Chaos Engineering --

def test_chaos_error_injection():
    chaos = ChaosEngine(seed=42)
    chaos.add_fault("api", FaultType.ERROR, probability=1.0, error_message="down")

    rule = chaos.should_inject("api")
    assert rule is not None

    try:
        chaos.inject(rule, "api")
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "down" in str(e)


def test_chaos_latency_injection():
    chaos = ChaosEngine()
    chaos.add_fault("slow", FaultType.LATENCY, latency_ms=50)

    rule = chaos.should_inject("slow")
    t0 = time.time()
    chaos.inject(rule, "slow")
    elapsed = (time.time() - t0) * 1000
    assert elapsed >= 40


def test_chaos_conditional_fault():
    chaos = ChaosEngine()
    chaos.add_fault(
        "db", FaultType.ERROR,
        condition=lambda args: "prod" in str(args),
    )

    assert chaos.should_inject("db", {"env": "staging"}) is None
    assert chaos.should_inject("db", {"env": "prod"}) is not None


def test_chaos_max_injections():
    chaos = ChaosEngine()
    chaos.add_fault("x", FaultType.ERROR, max_injections=2)

    r1 = chaos.should_inject("x")
    assert r1 is not None
    try:
        chaos.inject(r1, "x")
    except RuntimeError:
        pass

    r2 = chaos.should_inject("x")
    try:
        chaos.inject(r2, "x")
    except RuntimeError:
        pass

    assert chaos.should_inject("x") is None


def test_chaos_disabled():
    chaos = ChaosEngine(enabled=False)
    chaos.add_fault("api", FaultType.ERROR, probability=1.0)
    assert chaos.should_inject("api") is None


def test_chaos_wildcard():
    chaos = ChaosEngine()
    chaos.add_fault("*", FaultType.ERROR, probability=1.0)
    assert chaos.should_inject("any_tool") is not None
    assert chaos.should_inject("another_tool") is not None


def test_chaos_in_shield():
    chaos = ChaosEngine(seed=1)
    chaos.add_fault("flaky", FaultType.ERROR, probability=1.0, error_message="chaos!")

    shield = WarderBox(budget=10.0)
    shield.set_chaos(chaos)

    @shield.tool(cost=0.01)
    def flaky(msg: str):
        return msg

    result = flaky(msg="test")
    assert result.error is not None
    assert "chaos" in result.error.lower()


def test_chaos_stats():
    chaos = ChaosEngine()
    chaos.add_fault("a", FaultType.ERROR)
    chaos.add_fault("b", FaultType.LATENCY, latency_ms=10)

    r = chaos.should_inject("a")
    try:
        chaos.inject(r, "a")
    except RuntimeError:
        pass

    r = chaos.should_inject("b")
    chaos.inject(r, "b")

    s = chaos.stats()
    assert s["total_injections"] == 2
    assert s["by_type"]["error"] == 1
    assert s["by_type"]["latency"] == 1


def test_chaos_remove_faults():
    chaos = ChaosEngine()
    chaos.add_fault("x", FaultType.ERROR)
    chaos.add_fault("x", FaultType.LATENCY, latency_ms=10)
    removed = chaos.remove_faults("x")
    assert removed == 2
    assert chaos.should_inject("x") is None


# -- Cost Forecasting --

def test_forecast_empty():
    shield = WarderBox(budget=10.0)
    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    assert report.total_spent == 0.0
    assert report.budget == 10.0
    assert report.estimated_exhaustion is None


def test_forecast_with_actions():
    shield = WarderBox(budget=10.0)

    @shield.tool(cost=0.10)
    def search(q: str):
        return q

    for i in range(10):
        search(q=f"query {i}")

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()

    assert report.total_spent == 1.0
    assert report.remaining == 9.0
    assert report.total_actions == 10
    assert report.avg_cost_per_action == 0.1
    assert report.estimated_actions_remaining == 90
    assert report.burn_rate_per_hour > 0
    assert "search" in report.cost_by_tool


def test_forecast_summary():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.50)
    def expensive(x: str):
        return x

    expensive(x="a")
    expensive(x="b")

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    summary = report.summary
    assert "Spent" in summary
    assert "Budget" in summary


def test_forecast_trend_detection():
    shield = WarderBox(budget=100.0)

    @shield.tool(cost=0.01)
    def cheap(x: str):
        return x

    @shield.tool(cost=1.0)
    def expensive(x: str):
        return x

    cheap(x="a")
    cheap(x="b")
    expensive(x="c")
    expensive(x="d")

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    assert report.trend in ("increasing", "stable", "insufficient_data")


# -- Agent Scoring --

def test_scorer_perfect_agent():
    shield = WarderBox(budget=100.0)

    @shield.tool(cost=0.01)
    def safe_op(x: str):
        return x

    safe_op(x="a")
    safe_op(x="b")
    safe_op(x="c")

    scorer = AgentScorer(shield, agent_name="perfect_bot")
    report = scorer.score()
    assert report.overall_score >= 80
    assert report.grade in ("A", "B")
    assert report.agent_name == "perfect_bot"


def test_scorer_risky_agent():
    shield = WarderBox(budget=10.0)

    @shield.tool(cost=0.01)
    def danger(sql: str):
        return sql

    danger(sql="DROP TABLE users")
    danger(sql="TRUNCATE TABLE logs")
    danger(sql="DELETE FROM data")

    scorer = AgentScorer(shield)
    report = scorer.score()
    assert report.safety_score < 100
    assert report.total_actions > 0


def test_scorer_no_actions():
    shield = WarderBox(budget=10.0)
    scorer = AgentScorer(shield)
    report = scorer.score()
    assert report.overall_score == 100
    assert report.grade == "A"


def test_scorer_to_dict():
    shield = WarderBox(budget=10.0)

    @shield.tool(cost=0.01)
    def op(x: str):
        return x

    op(x="test")

    scorer = AgentScorer(shield, agent_name="bot")
    report = scorer.score()
    d = report.to_dict()
    assert d["agent_name"] == "bot"
    assert "overall_score" in d
    assert "grade" in d


def test_pool_scorer():
    pool = AgentPool(default_budget=10.0)
    a = pool.register("alpha", budget=10.0)
    b = pool.register("beta", budget=10.0)

    @a.tool(cost=0.01)
    def alpha_op(x: str):
        return x

    @b.tool(cost=0.01)
    def beta_op(x: str):
        return x

    alpha_op(x="a")
    alpha_op(x="b")
    beta_op(x="c")

    scorer = PoolScorer(pool)
    rankings = scorer.rank()
    assert len(rankings) == 2
    assert all(r.overall_score >= 0 for r in rankings)

    summary = scorer.summary()
    assert "Agent Rankings" in summary


if __name__ == "__main__":
    test_tracer_basic_span()
    test_tracer_context_manager()
    test_tracer_error_span()
    test_tracer_record_action()
    test_tracer_export_json()
    test_tracer_otlp_export()
    test_tracer_in_shield()
    test_tracer_stats()
    test_tracer_traces_grouping()
    test_chaos_error_injection()
    test_chaos_latency_injection()
    test_chaos_conditional_fault()
    test_chaos_max_injections()
    test_chaos_disabled()
    test_chaos_wildcard()
    test_chaos_in_shield()
    test_chaos_stats()
    test_chaos_remove_faults()
    test_forecast_empty()
    test_forecast_with_actions()
    test_forecast_summary()
    test_forecast_trend_detection()
    test_scorer_perfect_agent()
    test_scorer_risky_agent()
    test_scorer_no_actions()
    test_scorer_to_dict()
    test_pool_scorer()
    print("All Phase 8 tests passed!")
