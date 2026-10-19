"""Tests for fallback tool routing."""

from warderbox.fallback import FallbackManager, FallbackRoute


def test_register_and_get():
    fb = FallbackManager()
    fb.register("gpt4", "gpt3", fallback_cost=0.001)
    route = fb.get_fallback("gpt4")
    assert route is not None
    assert route.fallback_tool == "gpt3"
    assert route.fallback_cost == 0.001


def test_no_fallback():
    fb = FallbackManager()
    assert fb.get_fallback("unknown") is None


def test_has_fallback():
    fb = FallbackManager()
    fb.register("a", "b")
    assert fb.has_fallback("a") is True
    assert fb.has_fallback("c") is False


def test_unregister():
    fb = FallbackManager()
    fb.register("a", "b")
    assert fb.unregister("a") is True
    assert fb.has_fallback("a") is False


def test_unregister_nonexistent():
    fb = FallbackManager()
    assert fb.unregister("nope") is False


def test_fallback_fn():
    fb = FallbackManager()
    fb.register("gpt4", "gpt3", fallback_fn=lambda x: f"fallback:{x}")
    route = fb.get_fallback("gpt4")
    assert route.fallback_fn("test") == "fallback:test"


def test_max_fallbacks():
    fb = FallbackManager()
    fb.register("a", "b", max_fallbacks=2)
    fb.record_fallback("a")
    fb.record_fallback("a")
    assert fb.get_fallback("a") is None


def test_max_fallbacks_not_yet_exceeded():
    fb = FallbackManager()
    fb.register("a", "b", max_fallbacks=3)
    fb.record_fallback("a")
    assert fb.get_fallback("a") is not None


def test_reset_count():
    fb = FallbackManager()
    fb.register("a", "b", max_fallbacks=2)
    fb.record_fallback("a")
    fb.record_fallback("a")
    assert fb.get_fallback("a") is None
    fb.reset_count("a")
    assert fb.get_fallback("a") is not None


def test_condition_filter():
    fb = FallbackManager()
    fb.register("a", "b", condition="circuit_breaker")
    assert fb.get_fallback("a", reason="circuit_breaker") is not None
    assert fb.get_fallback("a", reason="timeout") is None


def test_condition_any():
    fb = FallbackManager()
    fb.register("a", "b", condition="any")
    assert fb.get_fallback("a", reason="circuit_breaker") is not None
    assert fb.get_fallback("a", reason="timeout") is not None


def test_stats():
    fb = FallbackManager()
    fb.register("gpt4", "gpt3", fallback_cost=0.001, max_fallbacks=10)
    fb.record_fallback("gpt4")
    fb.record_fallback("gpt4")
    stats = fb.stats()
    assert stats["registered_routes"] == 1
    assert stats["total_fallbacks_used"] == 2
    assert stats["routes"]["gpt4"]["times_used"] == 2
    assert stats["routes"]["gpt4"]["fallback_tool"] == "gpt3"


def test_multiple_routes():
    fb = FallbackManager()
    fb.register("gpt4", "gpt3")
    fb.register("claude_opus", "claude_haiku")
    assert fb.stats()["registered_routes"] == 2


def test_record_fallback_increments():
    fb = FallbackManager()
    fb.register("a", "b")
    assert fb.stats()["total_fallbacks_used"] == 0
    fb.record_fallback("a")
    assert fb.stats()["total_fallbacks_used"] == 1
