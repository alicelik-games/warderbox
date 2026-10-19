"""Tests for smart model routing."""

import pytest
from warderbox.routing import (
    ModelRouter,
    ComplexityClassifier,
    Complexity,
    ModelTier,
    RoutingDecision,
)


# --- ComplexityClassifier ---

def test_classifier_low_complexity():
    clf = ComplexityClassifier()
    level, score = clf.classify("Summarize this email for me")
    assert level == Complexity.LOW
    assert score < 0.36


def test_classifier_high_complexity():
    clf = ComplexityClassifier()
    level, score = clf.classify(
        "Design a distributed microservices architecture with security "
        "considerations and implement the database migration strategy"
    )
    assert level == Complexity.HIGH
    assert score > 0.65


def test_classifier_medium_complexity():
    clf = ComplexityClassifier()
    level, score = clf.classify("Write a Python function to process user data")
    assert level == Complexity.MEDIUM


def test_classifier_empty_prompt():
    clf = ComplexityClassifier()
    level, score = clf.classify("")
    assert level == Complexity.MEDIUM
    assert score == 0.5


def test_classifier_tool_override():
    clf = ComplexityClassifier()
    clf.set_tool_complexity("simple_lookup", 0.1)
    level, score = clf.classify("anything here", tool_name="simple_lookup")
    assert level == Complexity.LOW
    assert score == 0.1


def test_classifier_custom_rule():
    clf = ComplexityClassifier()
    clf.add_rule(r"URGENT", 0.9)
    level, score = clf.classify("URGENT: handle this immediately")
    assert level == Complexity.HIGH
    assert score == 0.9


def test_classifier_long_prompt():
    clf = ComplexityClassifier()
    long_text = "word " * 600
    level, score = clf.classify(long_text)
    assert score > 0.5


def test_classifier_code_block_boost():
    clf = ComplexityClassifier()
    prompt = "Fix this code:\n```python\ndef foo():\n    pass\n```\nand explain"
    level, score = clf.classify(prompt)
    assert score > 0.5


def test_classifier_token_estimate():
    clf = ComplexityClassifier()
    _, score_low = clf.classify("Hello", token_estimate=100)
    _, score_high = clf.classify("Hello", token_estimate=5000)
    assert score_high > score_low


# --- ModelRouter ---

def _make_router():
    router = ModelRouter()
    router.add_model("fast", cost_per_1k=0.00025, max_complexity=0.35)
    router.add_model("balanced", cost_per_1k=0.003, max_complexity=0.65)
    router.add_model("powerful", cost_per_1k=0.015, max_complexity=1.0)
    return router


def test_router_routes_simple_to_fast():
    router = _make_router()
    decision = router.route("Summarize this text")
    assert decision.model == "fast"
    assert decision.complexity == Complexity.LOW


def test_router_routes_complex_to_powerful():
    router = _make_router()
    decision = router.route(
        "Design a distributed system architecture with security analysis "
        "and implement the concurrent algorithm with performance optimization"
    )
    assert decision.model == "powerful"
    assert decision.complexity == Complexity.HIGH


def test_router_routes_medium_to_balanced():
    router = _make_router()
    decision = router.route("Write a REST API endpoint for user management")
    assert decision.model == "balanced"


def test_router_fallback_field():
    router = _make_router()
    decision = router.route("Summarize this")
    assert decision.fallback == "balanced"


def test_router_no_fallback_for_max():
    router = _make_router()
    decision = router.route(
        "Design and implement a distributed concurrent system"
    )
    assert decision.model == "powerful"
    assert decision.fallback is None


def test_router_tool_override():
    router = _make_router()
    router.override_tool("email_send", "fast")
    decision = router.route(tool_name="email_send")
    assert decision.model == "fast"
    assert "override" in decision.reason.lower()


def test_router_remove_override():
    router = _make_router()
    router.override_tool("email_send", "fast")
    router.remove_override("email_send")
    decision = router.route("Design a complex system", tool_name="email_send")
    assert decision.model != "fast" or "override" not in decision.reason.lower()


def test_router_min_complexity():
    router = _make_router()
    decision = router.route("Hello", min_complexity=Complexity.HIGH)
    assert decision.complexity == Complexity.HIGH
    assert decision.model == "powerful"


def test_router_stats():
    router = _make_router()
    router.route("Summarize this")
    router.route("Design a distributed architecture with security")
    router.route("Translate this text")

    stats = router.stats()
    assert stats.total_routed == 3
    assert sum(stats.by_model.values()) == 3
    assert stats.savings_percent > 0


def test_router_stats_savings():
    router = _make_router()
    for _ in range(10):
        router.route("Summarize this text")

    stats = router.stats()
    assert stats.savings_percent > 90


def test_router_reset_stats():
    router = _make_router()
    router.route("Hello")
    router.reset_stats()
    assert router.stats().total_routed == 0


def test_router_recent_decisions():
    router = _make_router()
    router.route("Task 1")
    router.route("Task 2")
    decisions = router.recent_decisions(limit=5)
    assert len(decisions) == 2
    assert "model" in decisions[0]


def test_router_no_models_raises():
    router = ModelRouter()
    with pytest.raises(ValueError, match="No models"):
        router.route("Hello")


def test_router_decision_to_dict():
    router = _make_router()
    decision = router.route("Summarize this")
    d = decision.to_dict()
    assert "model" in d
    assert "complexity" in d
    assert "complexity_score" in d
    assert "estimated_cost_per_1k" in d


def test_router_record_fallback():
    router = _make_router()
    router.route("Hello")
    router.record_fallback("fast", "balanced")
    assert router.stats().fallback_count == 1


def test_router_stats_summary():
    router = _make_router()
    router.route("Summarize this")
    router.route("Design a system with security")
    summary = router.stats().summary
    assert "Routing Stats" in summary
    assert "Savings" in summary


def test_router_stats_to_dict():
    router = _make_router()
    router.route("Hello")
    d = router.stats().to_dict()
    assert "total_routed" in d
    assert "savings_percent" in d


def test_router_model_with_metadata():
    router = ModelRouter()
    router.add_model(
        "haiku",
        cost_per_1k=0.00025,
        max_complexity=0.35,
        latency_ms=200,
        provider="anthropic",
        model_id="claude-haiku-5-5",
    )
    router.add_model(
        "opus",
        cost_per_1k=0.015,
        max_complexity=1.0,
        latency_ms=2000,
        provider="anthropic",
        model_id="claude-opus-5-5",
    )
    decision = router.route("Translate hello to French")
    assert decision.model == "haiku"
    assert decision.latency_ms == 200


def test_router_sorted_by_cost():
    router = ModelRouter()
    router.add_model("expensive", cost_per_1k=0.015, max_complexity=1.0)
    router.add_model("cheap", cost_per_1k=0.0001, max_complexity=0.5)
    decision = router.route("Summarize this short text for me please")
    assert decision.model == "cheap"
