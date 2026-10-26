"""Integration tests for the 5 new features (timeout, idempotency, output validation, fallback, quota)."""

import threading
import time

from warderbox import WarderBox
from warderbox.fallback import FallbackManager
from warderbox.idempotency import IdempotencyGuard
from warderbox.output_validation import OutputAction, OutputRule, OutputValidator
from warderbox.quota import QuotaManager, QuotaPeriod
from warderbox.timeout import TimeoutExecutor, TimeoutPolicy

# -- Timeout integration --

def test_shield_timeout_blocks_slow_tool():
    shield = WarderBox(budget=10.0)
    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=0.1)))

    @shield.tool(cost=0.01)
    def slow_tool():
        time.sleep(5)
        return "done"

    result = slow_tool()
    assert result.error is not None
    assert "timeout" in result.error.lower()


def test_shield_timeout_allows_fast_tool():
    shield = WarderBox(budget=10.0)
    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=5.0)))

    @shield.tool(cost=0.01)
    def fast_tool():
        return "quick"

    result = fast_tool()
    assert result.success
    assert result.output == "quick"


def test_shield_timeout_per_tool_override():
    policy = TimeoutPolicy(default_seconds=5.0, tool_timeouts={"slow_tool": 0.1})
    shield = WarderBox(budget=10.0)
    shield.set_timeout(TimeoutExecutor(policy))

    @shield.tool(cost=0.01)
    def slow_tool():
        time.sleep(5)

    @shield.tool(cost=0.01)
    def fast_tool():
        return "ok"

    assert slow_tool().error is not None
    assert fast_tool().success


# -- Idempotency integration --

def test_shield_idempotency_blocks_duplicate():
    shield = WarderBox(budget=10.0)
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    shield.set_idempotency(guard)

    call_count = 0

    @shield.tool(cost=0.01)
    def send_email(to="", body=""):
        nonlocal call_count
        call_count += 1
        return f"sent to {to}"

    r1 = send_email(to="a@b.com", body="hi")
    assert r1.success
    assert call_count == 1

    r2 = send_email(to="a@b.com", body="hi")
    assert r2.blocked
    assert "Duplicate" in r2.block_reason
    assert call_count == 1


def test_shield_idempotency_different_args_allowed():
    shield = WarderBox(budget=10.0)
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    shield.set_idempotency(guard)

    @shield.tool(cost=0.01)
    def send_email(to="", body=""):
        return f"sent to {to}"

    r1 = send_email(to="a@b.com", body="hello")
    r2 = send_email(to="c@d.com", body="world")
    assert r1.success
    assert r2.success


def test_shield_idempotency_unprotected_not_blocked():
    shield = WarderBox(budget=10.0)
    guard = IdempotencyGuard()
    shield.set_idempotency(guard)

    @shield.tool(cost=0.01)
    def query_db(sql=""):
        return "rows"

    r1 = query_db(sql="SELECT 1")
    r2 = query_db(sql="SELECT 1")
    assert r1.success
    assert r2.success


# -- Output validation integration --

def test_shield_output_validation_redacts():
    shield = WarderBox(budget=10.0)
    validator = OutputValidator()
    validator.add_rule(OutputRule(
        name="no_keys",
        pattern=r"sk-[a-zA-Z0-9]{20,}",
        action=OutputAction.REDACT,
    ))
    shield.set_output_validator(validator)

    @shield.tool(cost=0.01)
    def get_config():
        return "api_key: sk-abcdefghijklmnopqrstuvwxyz"

    result = get_config()
    assert result.success
    assert "sk-" not in str(result.output)
    assert "[REDACTED]" in str(result.output)


def test_shield_output_validation_blocks():
    shield = WarderBox(budget=10.0)
    validator = OutputValidator()
    validator.add_rule(OutputRule(
        name="no_ssn",
        pattern=r"\d{3}-\d{2}-\d{4}",
        action=OutputAction.BLOCK,
    ))
    shield.set_output_validator(validator)

    @shield.tool(cost=0.01)
    def lookup_user():
        return "SSN: 123-45-6789"

    result = lookup_user()
    assert not result.success
    assert "Output blocked" in result.error


def test_shield_output_validation_passes_clean():
    shield = WarderBox(budget=10.0)
    validator = OutputValidator()
    validator.add_rule(OutputRule(
        name="no_keys",
        pattern=r"sk-[a-zA-Z0-9]{20,}",
        action=OutputAction.BLOCK,
    ))
    shield.set_output_validator(validator)

    @shield.tool(cost=0.01)
    def safe_tool():
        return "no secrets here"

    result = safe_tool()
    assert result.success


# -- Fallback integration --

def test_shield_fallback_on_circuit_break():
    from warderbox.circuitbreaker import CircuitBreaker

    shield = WarderBox(budget=10.0)
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=60)
    shield.set_circuit_breaker(cb)

    fb = FallbackManager()
    fb.register(
        "expensive_api",
        "cheap_api",
        fallback_fn=lambda: "fallback result",
        fallback_cost=0.001,
        condition="any",
    )
    shield.set_fallback(fb)

    @shield.tool(cost=0.05)
    def expensive_api():
        raise RuntimeError("API down")

    expensive_api()
    result = expensive_api()
    assert result.success
    assert result.output == "fallback result"


def test_shield_fallback_not_triggered_on_success():
    from warderbox.circuitbreaker import CircuitBreaker

    shield = WarderBox(budget=10.0)
    cb = CircuitBreaker(failure_threshold=5, reset_timeout=60)
    shield.set_circuit_breaker(cb)

    fb = FallbackManager()
    fb.register("my_tool", "backup", fallback_fn=lambda: "backup")
    shield.set_fallback(fb)

    @shield.tool(cost=0.01)
    def my_tool():
        return "primary"

    result = my_tool()
    assert result.output == "primary"


# -- Quota integration --

def test_shield_quota_blocks_over_daily_limit():
    shield = WarderBox(budget=10.0)
    qm = QuotaManager()
    qm.set_quota("db_query", QuotaPeriod.DAILY, 3)
    shield.set_quota(qm)

    @shield.tool(cost=0.01)
    def db_query(sql=""):
        return "rows"

    for _ in range(3):
        r = db_query(sql="SELECT 1")
        assert r.success

    r = db_query(sql="SELECT 1")
    assert r.blocked
    assert "Quota exceeded" in r.block_reason


def test_shield_quota_global():
    shield = WarderBox(budget=10.0)
    qm = QuotaManager()
    qm.set_quota("global", QuotaPeriod.DAILY, 2)
    shield.set_quota(qm)

    @shield.tool(cost=0.01)
    def tool_a():
        return "a"

    @shield.tool(cost=0.01)
    def tool_b():
        return "b"

    assert tool_a().success
    assert tool_b().success
    assert tool_a().blocked


def test_shield_quota_allows_under_limit():
    shield = WarderBox(budget=10.0)
    qm = QuotaManager()
    qm.set_quota("tool_x", QuotaPeriod.MONTHLY, 1000)
    shield.set_quota(qm)

    @shield.tool(cost=0.01)
    def tool_x():
        return "ok"

    assert tool_x().success


# -- Combined features --

def test_all_five_features_together():
    shield = WarderBox(budget=10.0)

    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=5.0)))

    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")
    shield.set_idempotency(guard)

    validator = OutputValidator()
    validator.add_rule(OutputRule(
        name="no_keys", pattern=r"sk-\w{20,}", action=OutputAction.REDACT,
    ))
    shield.set_output_validator(validator)

    fb = FallbackManager()
    fb.register("broken_tool", "safe_tool", fallback_fn=lambda: "safe")
    shield.set_fallback(fb)

    qm = QuotaManager()
    qm.set_quota("send_email", QuotaPeriod.DAILY, 100)
    shield.set_quota(qm)

    @shield.tool(cost=0.01)
    def send_email(to="", body=""):
        return f"sent to {to}"

    r1 = send_email(to="a@b.com", body="test")
    assert r1.success

    r2 = send_email(to="a@b.com", body="test")
    assert r2.blocked
    assert "Duplicate" in r2.block_reason


def test_concurrent_timeout_and_idempotency():
    shield = WarderBox(budget=10.0)
    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=5.0)))
    guard = IdempotencyGuard()
    guard.add_protected_tool("api_call")
    shield.set_idempotency(guard)

    @shield.tool(cost=0.01)
    def api_call(query=""):
        return f"result for {query}"

    results = []

    def call(q):
        results.append(api_call(query=q))

    threads = [threading.Thread(target=call, args=(f"q{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r.success]
    assert len(successes) == 5


def test_version():
    from warderbox import __version__
    assert __version__ == "0.3.6"
