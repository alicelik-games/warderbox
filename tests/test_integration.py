"""Integration tests — all WarderBox features running together.

100 tests that exercise every module simultaneously:
shield, budget, risk, rollback, rate limiting, circuit breaker,
approval, capabilities, chaos, tracing, scoring, compliance,
masking, webhooks, retry, policy, multi-agent, forecast,
routing, anomaly detection, and dashboard API.
"""

import json
import os
import tempfile
import threading
import time
import urllib.request

from warderbox import (
    AgentPool,
    AgentScorer,
    AnomalyDetector,
    AnomalyType,
    ApprovalManager,
    CapabilityManager,
    ChaosEngine,
    CircuitBreaker,
    CircuitState,
    ComplianceReporter,
    CostForecaster,
    DataMasker,
    EventNotifier,
    FaultType,
    ModelRouter,
    PoolScorer,
    RateLimiter,
    RetryPolicy,
    RiskLevel,
    ToolLimit,
    Tracer,
    WarderBox,
    load_policy,
    rules_from_config,
    validate_policy,
)
from warderbox.dashboard import start_dashboard

# ============================================================
# Helpers
# ============================================================

def _full_shield(budget=10.0, db_path=None):
    shield = WarderBox(budget=budget, auto_snapshot=True, db_path=db_path)
    shield.set_masker(DataMasker())
    shield.set_circuit_breaker(CircuitBreaker(failure_threshold=3, reset_timeout=0.5))
    shield.set_tracer(Tracer(service_name="integration-test"))
    return shield


def _full_shield_with_chaos(budget=10.0):
    shield = _full_shield(budget=budget)
    chaos = ChaosEngine()
    shield.set_chaos(chaos)
    return shield, chaos


# ============================================================
# 1-10: Shield + Budget + Risk together
# ============================================================

def test_01_shield_blocks_critical_and_tracks_budget():
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def safe_query(sql: str) -> str:
        return f"OK: {sql}"

    @shield.tool(cost=0.10)
    def dangerous_query(sql: str) -> str:
        return f"OK: {sql}"

    r1 = safe_query(sql="SELECT * FROM users")
    assert r1.success
    assert shield._budget.spent == 0.05

    r2 = dangerous_query(sql="DROP TABLE users")
    assert r2.blocked
    assert "CRITICAL" in r2.block_reason


def test_02_budget_limit_stops_execution():
    shield = _full_shield(budget=0.10)

    @shield.tool(cost=0.05)
    def action(x: int) -> int:
        return x * 2

    r1 = action(x=1)
    assert r1.success
    r2 = action(x=2)
    assert r2.success
    r3 = action(x=3)
    assert r3.blocked
    assert "Budget exceeded" in r3.block_reason


def test_03_rollback_restores_state():
    shield = _full_shield()
    state = {"value": 0}

    @shield.tool(
        cost=0.01,
        snapshot_state=lambda: state.copy(),
        restore_state=lambda s: state.update(s),
    )
    def increment() -> int:
        state["value"] += 1
        return state["value"]

    increment()
    assert state["value"] == 1
    increment()
    assert state["value"] == 2
    shield.rollback()
    assert state["value"] == 1


def test_04_test_mode_no_side_effects():
    shield = WarderBox(budget=5.0, mode="test")
    calls = []

    @shield.tool(cost=0.10)
    def real_action(data: str) -> str:
        calls.append(data)
        return "done"

    result = real_action(data="test")
    assert result.success
    assert "[TEST MODE]" in result.output
    assert len(calls) == 0


def test_05_history_records_all_actions():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def action(n: int) -> int:
        return n

    for i in range(5):
        action(n=i)

    h = shield.history()
    assert len(h) == 5
    assert all(entry["success"] for entry in h)


def test_06_stats_correct_after_mixed_actions():
    shield = _full_shield(budget=1.0)

    @shield.tool(cost=0.01)
    def safe(x: int) -> int:
        return x

    @shield.tool(cost=0.01)
    def dangerous(sql: str) -> str:
        return sql

    for i in range(5):
        safe(x=i)
    dangerous(sql="DROP TABLE x")

    stats = shield.stats()
    assert stats["total_actions"] == 6
    assert stats["successful"] == 5
    assert stats["blocked"] == 1


def test_07_multiple_risk_levels():
    shield = _full_shield()

    @shield.tool(cost=0.01, risk=RiskLevel.LOW)
    def low_risk() -> str:
        return "low"

    @shield.tool(cost=0.01, risk=RiskLevel.MEDIUM)
    def medium_risk() -> str:
        return "medium"

    r1 = low_risk()
    r2 = medium_risk()
    assert r1.success
    assert r2.success


def test_08_on_block_callback():
    decisions = []
    shield = WarderBox(budget=5.0, on_block=lambda a, r: (decisions.append("asked"), False)[1])

    @shield.tool(cost=0.01, risk=RiskLevel.HIGH)
    def risky() -> str:
        return "done"

    result = risky()
    assert result.blocked
    assert len(decisions) == 1


def test_09_action_result_properties():
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def good() -> str:
        return "ok"

    @shield.tool(cost=0.05)
    def bad(sql: str) -> str:
        return sql

    r1 = good()
    assert r1.success is True
    assert r1.blocked is False
    assert r1.output == "ok"

    r2 = bad(sql="DROP TABLE x")
    assert r2.success is False
    assert r2.blocked is True


def test_10_shield_from_config():
    config = {
        "budget": 5.0,
        "mode": "test",
        "auto_snapshot": True,
        "global_rpm": 100,
        "rules": [
            {"name": "block_prod", "match": {"args_regex": "prod"}, "risk": "critical"}
        ],
    }
    shield = WarderBox.from_config(config)
    assert shield.is_test_mode


# ============================================================
# 11-20: Rate Limiting + Circuit Breaker
# ============================================================

def test_11_rate_limit_blocks_excess():
    shield = WarderBox(budget=5.0, global_rpm=5)

    @shield.tool(cost=0.01)
    def action(n: int) -> int:
        return n

    results = [action(n=i) for i in range(10)]
    blocked = [r for r in results if r.blocked]
    assert len(blocked) > 0


def test_12_per_tool_rate_limit():
    shield = WarderBox(
        budget=5.0,
        tool_limits={"limited": ToolLimit(rpm=3)},
    )

    @shield.tool(cost=0.01)
    def limited(n: int) -> int:
        return n

    @shield.tool(cost=0.01)
    def unlimited(n: int) -> int:
        return n

    for i in range(5):
        limited(n=i)
    for i in range(5):
        unlimited(n=i)

    h = shield.history()
    limited_blocked = [e for e in h if e["tool"] == "limited" and e["blocked"]]
    unlimited_blocked = [e for e in h if e["tool"] == "unlimited" and e["blocked"]]
    assert len(limited_blocked) > 0
    assert len(unlimited_blocked) == 0


def test_13_circuit_breaker_opens():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def failing() -> str:
        raise RuntimeError("fail")

    for _ in range(3):
        failing()

    result = failing()
    assert result.blocked
    assert "Circuit breaker" in result.block_reason


def test_14_circuit_breaker_recovery():
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.1, success_threshold=1)
    shield = WarderBox(budget=5.0)
    shield.set_circuit_breaker(cb)

    @shield.tool(cost=0.01)
    def action() -> str:
        return "ok"

    cb.record_failure("action")
    cb.record_failure("action")
    assert cb.get_state("action") == CircuitState.OPEN

    time.sleep(0.15)
    ok, _ = cb.should_allow("action")
    assert ok
    cb.record_success("action")
    assert cb.get_state("action") == CircuitState.CLOSED


def test_15_rate_limit_and_circuit_breaker_together():
    shield = WarderBox(budget=5.0, global_rpm=20)
    shield.set_circuit_breaker(CircuitBreaker(failure_threshold=3))

    @shield.tool(cost=0.01)
    def action() -> str:
        raise RuntimeError("err")

    results = [action() for _ in range(10)]
    errors = [r for r in results if r.error and not r.blocked]
    blocked = [r for r in results if r.blocked]
    assert len(errors) > 0
    assert len(blocked) > 0


def test_16_circuit_breaker_stats():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("tool_a")
    cb.record_failure("tool_a")
    cb.record_success("tool_b")
    stats = cb.stats()
    assert stats["tool_a"]["failure_count"] == 2
    assert stats["tool_b"]["state"] == "closed"


def test_17_rate_limiter_stats():
    rl = RateLimiter(global_rpm=100, tool_limits={"api": ToolLimit(rpm=10)})
    for _ in range(5):
        rl.record("api")
    rl.record("other")
    stats = rl.stats()
    assert stats["global_last_minute"] == 6
    assert stats["by_tool"]["api"]["last_minute"] == 5


def test_18_circuit_breaker_callbacks():
    opened = []
    cb = CircuitBreaker(
        failure_threshold=2,
        on_open=lambda tool: opened.append(tool),
    )
    cb.record_failure("test")
    cb.record_failure("test")
    assert "test" in opened


def test_19_tool_limit_rph():
    tl = ToolLimit(rph=5)
    assert tl.rph == 5


def test_20_rate_limiter_prune():
    rl = RateLimiter(global_rpm=100)
    for _ in range(10):
        rl.record("tool")
    stats = rl.stats()
    assert stats["global_last_minute"] == 10


# ============================================================
# 21-30: Approval + Capabilities
# ============================================================

def test_21_approval_blocks_pending():
    approver = ApprovalManager(default_timeout=0.05)
    approver.require_approval("send_email", auto_deny=True)
    shield = WarderBox(budget=5.0)
    shield.set_approval(approver)

    ran = []

    @shield.tool(cost=0.01)
    def send_email(to: str) -> str:
        ran.append(to)
        return f"sent to {to}"

    result = send_email(to="user@test.com")
    # No approver responded -> auto-deny on timeout -> blocked, tool never ran.
    assert result.blocked is True
    assert ran == []


def test_22_capabilities_allow_deny():
    caps = CapabilityManager()
    caps.grant("bot", allow=["search"], deny=["delete"])
    shield = WarderBox(budget=5.0)
    shield.set_capabilities(caps)
    shield._agent_name = "bot"

    @shield.tool(cost=0.01)
    def search(q: str) -> str:
        return q

    @shield.tool(cost=0.01)
    def delete(id: int) -> str:
        return "deleted"

    r1 = search(q="test")
    assert r1.success
    r2 = delete(id=1)
    assert r2.blocked
    assert "Capability denied" in r2.block_reason


def test_23_capabilities_args_constraint():
    caps = CapabilityManager()
    caps.add_constraint("bot", "run_sql", args_deny=["DROP", "TRUNCATE"])
    shield = WarderBox(budget=5.0)
    shield.set_capabilities(caps)
    shield._agent_name = "bot"

    @shield.tool(cost=0.01)
    def run_sql(sql: str) -> str:
        return sql

    r1 = run_sql(sql="SELECT 1")
    assert r1.success
    r2 = run_sql(sql="DROP TABLE x")
    assert r2.blocked


def test_24_capabilities_max_cost():
    caps = CapabilityManager()
    caps.grant("bot", allow=["expensive"])
    caps.add_constraint("bot", "expensive", max_cost=0.05)
    shield = WarderBox(budget=5.0)
    shield.set_capabilities(caps)
    shield._agent_name = "bot"

    @shield.tool(cost=0.10)
    def expensive() -> str:
        return "done"

    result = expensive()
    assert result.blocked


def test_25_capabilities_revoke():
    # After all capabilities are revoked the agent is unknown again and,
    # under the secure fail-closed default, must be denied.
    caps = CapabilityManager()
    caps.grant("bot", allow=["tool_a"])
    caps.revoke("bot")
    shield = WarderBox(budget=5.0, agent_name="bot")
    shield.set_capabilities(caps)

    @shield.tool(cost=0.01)
    def tool_a() -> str:
        return "ok"

    result = tool_a()
    assert result.blocked


def test_26_approval_with_capabilities():
    # Capability allows the tool, but an un-answered approval policy must
    # still gate it: both checks have to pass.
    caps = CapabilityManager()
    caps.grant("bot", allow=["action"])
    approver = ApprovalManager(default_timeout=0.05)
    approver.require_approval("action", auto_deny=True)

    shield = WarderBox(budget=5.0, agent_name="bot")
    shield.set_capabilities(caps)
    shield.set_approval(approver)

    ran = []

    @shield.tool(cost=0.01)
    def action() -> str:
        ran.append(1)
        return "ok"

    result = action()
    assert result.blocked is True
    assert ran == []


def test_27_capabilities_stats():
    caps = CapabilityManager()
    caps.grant("a", allow=["x"])
    caps.grant("b", deny=["y"])
    stats = caps.stats()
    assert len(stats) == 2
    assert "a" in stats and "b" in stats


def test_28_approval_manager_stats():
    approver = ApprovalManager()
    approver.require_approval("email")
    approver.require_approval("deploy")
    stats = approver.stats()
    assert stats["total_requests"] == 0
    assert len(approver._policies) == 2


def test_29_approval_risk_based():
    approver = ApprovalManager(default_timeout=0.01)
    approver.require_approval_for_risk("HIGH")
    assert "__risk__HIGH" in approver._policies


def test_30_capabilities_unknown_agent_fails_closed():
    caps = CapabilityManager()
    caps.grant("known", allow=["tool"])
    ok, reason = caps.check("unknown_agent", "any_tool")
    assert not ok

    permissive = CapabilityManager(fail_closed=False)
    permissive.grant("known", allow=["tool"])
    ok2, _ = permissive.check("unknown_agent", "any_tool")
    assert ok2


# ============================================================
# 31-40: Chaos + Tracing + Retry
# ============================================================

def test_31_chaos_error_injection():
    shield, chaos = _full_shield_with_chaos()
    chaos.add_fault("fragile", FaultType.ERROR, probability=1.0)

    @shield.tool(cost=0.01)
    def fragile() -> str:
        return "ok"

    result = fragile()
    assert result.error is not None


def test_32_chaos_latency_injection():
    shield, chaos = _full_shield_with_chaos()
    chaos.add_fault("slow", FaultType.LATENCY, latency_ms=50, probability=1.0)

    @shield.tool(cost=0.01)
    def slow() -> str:
        return "ok"

    t0 = time.time()
    result = slow()
    elapsed = (time.time() - t0) * 1000
    assert result.success
    assert elapsed >= 40


def test_33_chaos_conditional_fault():
    shield, chaos = _full_shield_with_chaos()
    chaos.add_fault("query", FaultType.ERROR, condition=lambda args: "prod" in str(args))

    @shield.tool(cost=0.01)
    def query(env: str) -> str:
        return f"result from {env}"

    r1 = query(env="dev")
    assert r1.success
    r2 = query(env="prod")
    assert r2.error is not None


def test_34_chaos_disabled():
    shield, chaos = _full_shield_with_chaos()
    chaos.disable()
    chaos.add_fault("tool", FaultType.ERROR, probability=1.0)

    @shield.tool(cost=0.01)
    def tool() -> str:
        return "ok"

    result = tool()
    assert result.success


def test_35_tracing_spans():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def action() -> str:
        return "ok"

    action()
    action()

    tracer = shield._tracer
    spans = tracer._spans
    assert len(spans) >= 2
    assert all(s.status == "ok" for s in spans)


def test_36_tracing_error_span():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def bad() -> str:
        raise ValueError("boom")

    bad()
    spans = shield._tracer._spans
    error_spans = [s for s in spans if s.status == "error"]
    assert len(error_spans) >= 1


def test_37_tracing_export_json():
    tracer = Tracer(service_name="test")
    span = tracer.start_span("test_op")
    tracer.end_span(span, "ok")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        tracer.export_json(path)
        with open(path) as f:
            data = json.load(f)
        assert len(data) >= 1
    finally:
        os.unlink(path)


def test_38_retry_with_backoff():
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("fail")
        return "success"

    shield = _full_shield()

    @shield.tool(cost=0.01, retry=RetryPolicy(max_retries=5, backoff_base=0.01))
    def action() -> str:
        return flaky()

    result = action()
    assert result.success
    assert len(attempts) == 3


def test_39_retry_exhausted():
    def always_fail(*a, **kw):
        raise RuntimeError("nope")

    shield = _full_shield()

    @shield.tool(cost=0.01, retry=RetryPolicy(max_retries=2, backoff_base=0.01))
    def action() -> str:
        return always_fail()

    result = action()
    assert result.error is not None


def test_40_chaos_with_tracing():
    shield, chaos = _full_shield_with_chaos()
    chaos.add_fault("tracked", FaultType.ERROR, probability=1.0)

    @shield.tool(cost=0.01)
    def tracked() -> str:
        return "ok"

    tracked()
    spans = shield._tracer._spans
    assert len(spans) >= 1
    assert any(s.status == "error" for s in spans)


# ============================================================
# 41-50: Masking + Logging + Compliance
# ============================================================

def test_41_masking_email():
    masker = DataMasker()
    result = masker.mask({"to": "user@example.com"})
    assert "user@example.com" not in str(result)


def test_42_masking_api_key():
    masker = DataMasker()
    result = masker.mask({"key": "sk-abc123def456ghi789jklmnop"})
    assert "sk-abc123" not in str(result)


def test_43_masking_custom_pattern():
    masker = DataMasker()
    masker.add_pattern("project", r"PROJ-\d{6}", "PROJ-******")
    result = masker.mask({"id": "PROJ-123456"})
    assert result["id"] == "PROJ-******"


def test_44_logging_to_sqlite():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        shield = WarderBox(budget=5.0, db_path=db_path)

        @shield.tool(cost=0.05)
        def action() -> str:
            return "done"

        action()
        action()
        assert shield._logger is not None
    finally:
        os.unlink(db_path)


def test_45_compliance_report():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        shield = WarderBox(budget=5.0, db_path=db_path)

        @shield.tool(cost=0.05)
        def action() -> str:
            return "ok"

        for _ in range(5):
            action()

        reporter = ComplianceReporter(db_path=db_path)
        report = reporter.generate_report()
        assert report.total_actions == 5
    finally:
        os.unlink(db_path)


def test_46_compliance_json_export():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    json_path = db_path + ".json"
    try:
        shield = WarderBox(budget=5.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def action() -> str:
            return "ok"

        action()
        reporter = ComplianceReporter(db_path=db_path)
        reporter.generate_report()
        reporter.export_json(json_path)
        with open(json_path) as f:
            data = json.load(f)
        assert "total_actions" in data
    finally:
        for p in [db_path, json_path]:
            if os.path.exists(p):
                os.unlink(p)


def test_47_compliance_html():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        shield = WarderBox(budget=5.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def action() -> str:
            return "ok"

        action()
        reporter = ComplianceReporter(db_path=db_path)
        reporter.generate_report()
        html = reporter.to_html()
        assert "<html" in html.lower() or "compliance" in html.lower()
    finally:
        os.unlink(db_path)


def test_48_masking_in_shield():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def action(email: str) -> str:
        return f"sent to {email}"

    result = action(email="secret@example.com")
    assert result.success


def test_49_logger_with_masker():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        masker = DataMasker()
        shield = WarderBox(budget=5.0, db_path=db_path)
        shield.set_masker(masker)

        @shield.tool(cost=0.01)
        def action(data: str) -> str:
            return data

        action(data="email: test@example.com")
        assert shield._logger is not None
    finally:
        os.unlink(db_path)


def test_50_masking_stats():
    masker = DataMasker()
    assert len(masker._patterns) > 0


# ============================================================
# 51-60: Multi-Agent + Scoring
# ============================================================

def test_51_agent_pool_basic():
    pool = AgentPool(default_budget=5.0)
    pool.register("researcher", budget=3.0)
    pool.register("coder", budget=10.0)
    assert len(pool.agents) == 2
    assert "researcher" in pool


def test_52_agent_pool_isolated_budgets():
    pool = AgentPool(default_budget=5.0)
    r = pool.register("r", budget=1.0)
    c = pool.register("c", budget=1.0)

    @r.tool(cost=0.50)
    def research() -> str:
        return "found"

    @c.tool(cost=0.30)
    def code() -> str:
        return "coded"

    research()
    research()
    code()

    assert pool["r"]._budget.spent == 1.0
    assert pool["c"]._budget.spent == 0.30


def test_53_agent_pool_stats():
    pool = AgentPool(default_budget=5.0)
    r = pool.register("a", budget=5.0)
    c = pool.register("b", budget=5.0)

    @r.tool(cost=0.1)
    def a_action() -> str:
        return "ok"

    @c.tool(cost=0.2)
    def b_action() -> str:
        return "ok"

    a_action()
    b_action()

    stats = pool.stats()
    assert stats["total_agents"] == 2
    assert stats["total_actions"] == 2
    assert stats["total_spent"] == 0.3


def test_54_agent_pool_over_budget():
    pool = AgentPool()
    s = pool.register("small", budget=0.10)

    @s.tool(cost=0.05)
    def action() -> str:
        return "ok"

    action()
    action()
    assert "small" in pool.over_budget()


def test_55_agent_scorer_perfect():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def good() -> str:
        return "ok"

    for _ in range(10):
        good()

    scorer = AgentScorer(shield, agent_name="good-bot")
    report = scorer.score()
    assert report.overall_score >= 80
    assert report.grade in ("A", "B")


def test_56_agent_scorer_risky():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def danger(sql: str) -> str:
        return sql

    for _ in range(5):
        danger(sql="DROP TABLE x")

    scorer = AgentScorer(shield, agent_name="risky-bot")
    report = scorer.score()
    assert report.safety_score < 100


def test_57_pool_scorer_ranking():
    pool = AgentPool(default_budget=5.0)
    good = pool.register("good", budget=5.0)
    bad = pool.register("bad", budget=5.0)

    @good.tool(cost=0.01)
    def good_action() -> str:
        return "ok"

    @bad.tool(cost=0.01)
    def bad_action(sql: str) -> str:
        return sql

    for _ in range(5):
        good_action()
    for _ in range(5):
        bad_action(sql="DROP TABLE x")

    ps = PoolScorer(pool)
    rankings = ps.rank()
    assert rankings[0].agent_name == "good"


def test_58_scorer_to_dict():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def action() -> str:
        return "ok"

    action()
    scorer = AgentScorer(shield)
    d = scorer.score().to_dict()
    assert "overall_score" in d
    assert "grade" in d


def test_59_pool_scorer_summary():
    pool = AgentPool(default_budget=5.0)
    pool.register("alpha")
    pool.register("beta")
    ps = PoolScorer(pool)
    summary = ps.summary()
    assert "Rankings" in summary


def test_60_agent_pool_history():
    pool = AgentPool(default_budget=5.0)
    s = pool.register("agent1")

    @s.tool(cost=0.01)
    def act() -> str:
        return "ok"

    act()
    h = pool.history("agent1")
    assert len(h) == 1


# ============================================================
# 61-70: Webhooks + Events + Policy
# ============================================================

def test_61_webhook_callback():
    events = []
    notifier = EventNotifier()
    notifier.on("action_blocked", lambda e, d: events.append((e, d)))

    shield = WarderBox(budget=5.0)
    shield._notifier = notifier

    @shield.tool(cost=0.01)
    def danger(sql: str) -> str:
        return sql

    danger(sql="DROP TABLE x")
    assert len(events) == 1
    assert events[0][0] == "action_blocked"


def test_62_webhook_multiple_events():
    events = []
    notifier = EventNotifier()
    notifier.on("action_completed", lambda e, d: events.append(e))
    notifier.on("action_blocked", lambda e, d: events.append(e))

    shield = WarderBox(budget=5.0)
    shield._notifier = notifier

    @shield.tool(cost=0.01)
    def safe() -> str:
        return "ok"

    @shield.tool(cost=0.01)
    def bad(sql: str) -> str:
        return sql

    safe()
    bad(sql="DROP TABLE x")
    assert "action_completed" in events
    assert "action_blocked" in events


def test_63_budget_exceeded_event():
    events = []
    notifier = EventNotifier()
    notifier.on("budget_exceeded", lambda e, d: events.append(d))

    shield = WarderBox(budget=0.05)
    shield._notifier = notifier

    @shield.tool(cost=0.10)
    def expensive() -> str:
        return "ok"

    expensive()
    assert len(events) == 1


def test_64_policy_load_json():
    policy = {
        "budget": 10.0,
        "mode": "production",
        "rules": [
            {"name": "block_prod", "match": {"args_regex": "production"}, "risk": "critical"}
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(policy, f)
        path = f.name
    try:
        shield = load_policy(path)
        assert shield._budget.budget == 10.0
    finally:
        os.unlink(path)


def test_65_policy_validate():
    policy = {
        "budget": 5.0,
        "mode": "production",
        "rules": [],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(policy, f)
        path = f.name
    try:
        errors = validate_policy(path)
        assert len(errors) == 0
    finally:
        os.unlink(path)


def test_66_policy_with_webhooks():
    policy = {
        "budget": 5.0,
        "webhooks": [
            {"url": "https://example.com/hook", "events": ["action_blocked"]}
        ],
    }
    shield = WarderBox.from_config(policy)
    assert len(shield._notifier._webhooks) == 1


def test_67_rules_from_config():
    rule_list = [
        {"name": "r1", "match": {"tool": "danger"}, "risk": "critical"},
    ]
    rules = rules_from_config(rule_list)
    assert len(rules) >= 1


def test_68_event_notifier_emit():
    events = []
    notifier = EventNotifier()
    notifier.on("test", lambda e, d: events.append(e))
    notifier.emit("test", {})
    notifier.emit("test", {})
    assert len(events) == 2
    assert events[0] == "test"


def test_69_notifier_add_webhook_url():
    notifier = EventNotifier()
    notifier.add_webhook(url="https://example.com/hook", events={"action_blocked"})
    assert len(notifier._webhooks) == 1


def test_70_policy_custom_rules_execute():
    shield = WarderBox.from_config({
        "budget": 5.0,
        "rules": [
            {"name": "no_prod", "match": {"args_regex": "production"}, "risk": "critical"}
        ],
    })

    @shield.tool(cost=0.01)
    def query(env: str) -> str:
        return env

    r1 = query(env="staging")
    assert r1.success
    r2 = query(env="production")
    assert r2.blocked


# ============================================================
# 71-80: Forecast + Routing
# ============================================================

def test_71_forecast_basic():
    shield = _full_shield()

    @shield.tool(cost=0.10)
    def action() -> str:
        return "ok"

    for _ in range(10):
        action()

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    assert report.total_spent == 1.0
    assert report.total_actions == 10


def test_72_forecast_exhaustion():
    shield = _full_shield(budget=2.0)

    @shield.tool(cost=0.10)
    def action() -> str:
        return "ok"

    for _ in range(10):
        action()

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    assert report.estimated_actions_remaining is not None


def test_73_forecast_cost_by_tool():
    shield = _full_shield()

    @shield.tool(cost=0.10)
    def expensive() -> str:
        return "ok"

    @shield.tool(cost=0.01)
    def cheap() -> str:
        return "ok"

    for _ in range(5):
        expensive()
    for _ in range(5):
        cheap()

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    assert "expensive" in report.cost_by_tool
    assert report.cost_by_tool["expensive"] > report.cost_by_tool["cheap"]


def test_74_forecast_summary_text():
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def action() -> str:
        return "ok"

    for _ in range(5):
        action()

    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    assert "Cost Forecast" in report.summary


def test_75_router_three_tier():
    router = ModelRouter()
    router.add_model("haiku", cost_per_1k=0.00025, max_complexity=0.35)
    router.add_model("sonnet", cost_per_1k=0.003, max_complexity=0.65)
    router.add_model("opus", cost_per_1k=0.015, max_complexity=1.0)

    d1 = router.route("Summarize this text")
    d2 = router.route("Write a REST API")
    d3 = router.route("Design a distributed system with security analysis")

    assert d1.model == "haiku"
    assert d2.model == "sonnet"
    assert d3.model == "opus"


def test_76_router_savings_calculation():
    router = ModelRouter()
    router.add_model("cheap", cost_per_1k=0.001, max_complexity=0.5)
    router.add_model("expensive", cost_per_1k=0.1, max_complexity=1.0)

    for _ in range(20):
        router.route("Summarize this short text for me")

    stats = router.stats()
    assert stats.savings_percent > 50


def test_77_router_with_shield():
    shield = _full_shield()
    router = ModelRouter()
    router.add_model("fast", cost_per_1k=0.001, max_complexity=0.5)
    router.add_model("smart", cost_per_1k=0.01, max_complexity=1.0)

    @shield.tool(cost=0.05)
    def action(prompt: str) -> str:
        decision = router.route(prompt)
        return f"Used {decision.model}"

    r = action(prompt="Hello world")
    assert r.success
    assert router.stats().total_routed == 1


def test_78_classifier_with_router():
    router = ModelRouter()
    router.add_model("a", cost_per_1k=0.001, max_complexity=0.35)
    router.add_model("b", cost_per_1k=0.01, max_complexity=1.0)
    router.classifier.set_tool_complexity("simple_tool", 0.1)

    d = router.route(tool_name="simple_tool")
    assert d.model == "a"


def test_79_router_override_tool():
    router = ModelRouter()
    router.add_model("cheap", cost_per_1k=0.001, max_complexity=0.5)
    router.add_model("expensive", cost_per_1k=0.1, max_complexity=1.0)
    router.override_tool("critical_tool", "expensive")

    d = router.route("Simple task", tool_name="critical_tool")
    assert d.model == "expensive"


def test_80_forecast_to_dict():
    shield = _full_shield()

    @shield.tool(cost=0.01)
    def action() -> str:
        return "ok"

    action()
    forecaster = CostForecaster(shield)
    d = forecaster.forecast().to_dict()
    assert "total_spent" in d
    assert "burn_rate_per_hour" in d


# ============================================================
# 81-90: Anomaly Detection + Combined
# ============================================================

def test_81_anomaly_cost_spike():
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=100)
    for _ in range(20):
        detector.record("tool", 0.03)
    alerts = detector.record("tool", 10.0)
    assert any(a.type == AnomalyType.COST_SPIKE for a in alerts)


def test_82_anomaly_loop_detection():
    detector = AnomalyDetector(loop_threshold=5)
    alerts = []
    for _ in range(10):
        alerts.extend(detector.record("stuck", 0.01))
    assert any(a.type == AnomalyType.LOOP_DETECTED for a in alerts)


def test_83_anomaly_with_shield():
    shield = _full_shield()
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=100)

    @shield.tool(cost=0.03)
    def action() -> str:
        return "ok"

    for _ in range(20):
        r = action()
        if r.success:
            detector.record("action", r.cost)

    spike_alerts = detector.record("action", 50.0)
    assert len(spike_alerts) > 0


def test_84_anomaly_burn_rate():
    detector = AnomalyDetector(burn_rate_multiplier=2.0)
    detector.set_baseline_burn_rate(0.10)
    now = time.time()
    alerts = []
    for i in range(20):
        alerts.extend(detector.record("tool", 0.50, timestamp=now + i * 0.5))
    burn_alerts = [a for a in alerts if a.type == AnomalyType.BUDGET_BURN]
    assert len(burn_alerts) > 0


def test_85_anomaly_callback():
    received = []
    detector = AnomalyDetector(
        sensitivity=2.0,
        loop_threshold=100,
        on_alert=lambda a: received.append(a),
    )
    for _ in range(20):
        detector.record("tool", 0.01)
    detector.record("tool", 100.0)
    assert len(received) > 0


def test_86_anomaly_reset():
    detector = AnomalyDetector(loop_threshold=100)
    for _ in range(10):
        detector.record("tool", 0.01)
    detector.reset()
    assert detector.stats()["total_records"] == 0


def test_87_anomaly_stats():
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=100)
    for _ in range(10):
        detector.record("tool", 0.05)
    detector.record("tool", 100.0)
    stats = detector.stats()
    assert stats["total_records"] == 11
    assert stats["total_alerts"] > 0


def test_88_all_modules_together():
    """Full integration: shield + budget + CB + tracing + masking + routing + anomaly."""
    shield = _full_shield(budget=5.0)
    router = ModelRouter()
    router.add_model("fast", cost_per_1k=0.001, max_complexity=0.5)
    router.add_model("smart", cost_per_1k=0.01, max_complexity=1.0)
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=50)

    @shield.tool(cost=0.05)
    def agent_action(prompt: str) -> str:
        decision = router.route(prompt)
        detector.record("agent_action", 0.05)
        return f"model={decision.model}"

    for i in range(10):
        r = agent_action(prompt=f"Task {i}: summarize text")
        assert r.success

    assert shield._budget.spent == 0.50
    assert router.stats().total_routed == 10
    assert detector.stats()["total_records"] == 10

    scorer = AgentScorer(shield, agent_name="integrated-bot")
    report = scorer.score()
    assert report.grade in ("A", "B")

    forecaster = CostForecaster(shield)
    forecast = forecaster.forecast()
    assert forecast.total_spent == 0.50


def test_89_full_pipeline_with_blocks():
    """Integration: safe + blocked + error actions through full pipeline."""
    shield = _full_shield(budget=5.0)
    router = ModelRouter()
    router.add_model("model", cost_per_1k=0.01, max_complexity=1.0)
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=50)

    @shield.tool(cost=0.05)
    def safe_action() -> str:
        return "ok"

    @shield.tool(cost=0.05)
    def blocked_action(sql: str) -> str:
        return sql

    @shield.tool(cost=0.05)
    def error_action() -> str:
        raise RuntimeError("boom")

    for _ in range(5):
        safe_action()
        router.route("Simple task")
        detector.record("safe", 0.05)

    blocked_action(sql="DROP TABLE users")
    error_action()

    stats = shield.stats()
    assert stats["successful"] == 5
    assert stats["blocked"] == 1
    assert stats["errors"] == 1
    assert stats["total_actions"] == 7


def test_90_multi_agent_with_routing_and_anomaly():
    """Multi-agent pool with routing and anomaly detection per agent."""
    pool = AgentPool(default_budget=5.0)
    r = pool.register("researcher", budget=3.0)
    c = pool.register("coder", budget=10.0)

    router_r = ModelRouter()
    router_r.add_model("fast", cost_per_1k=0.001, max_complexity=0.5)
    router_r.add_model("smart", cost_per_1k=0.01, max_complexity=1.0)

    router_c = ModelRouter()
    router_c.add_model("fast", cost_per_1k=0.001, max_complexity=0.5)
    router_c.add_model("smart", cost_per_1k=0.01, max_complexity=1.0)

    @r.tool(cost=0.05)
    def research(q: str) -> str:
        router_r.route(q)
        return f"found: {q}"

    @c.tool(cost=0.10)
    def code(task: str) -> str:
        router_c.route(task)
        return f"coded: {task}"

    for i in range(5):
        research(q=f"topic {i}")
    for i in range(3):
        code(task=f"implement feature {i}")

    ps = PoolScorer(pool)
    rankings = ps.rank()
    assert len(rankings) == 2

    pool_stats = pool.stats()
    assert pool_stats["total_actions"] == 8
    assert router_r.stats().total_routed == 5
    assert router_c.stats().total_routed == 3


# ============================================================
# 91-100: Dashboard API + Edge Cases
# ============================================================

def test_91_dashboard_api_stats():
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def action() -> str:
        return "ok"

    for _ in range(3):
        action()

    server = start_dashboard(shield, port=0, open_browser=False, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats")
        data = json.loads(resp.read())
        assert data["total_actions"] == 3
    finally:
        server.shutdown()


def test_92_dashboard_api_history():
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def action() -> str:
        return "ok"

    action()
    server = start_dashboard(shield, port=0, open_browser=False, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/history")
        data = json.loads(resp.read())
        assert len(data) == 1
    finally:
        server.shutdown()


def test_93_dashboard_api_finops():
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def action() -> str:
        return "ok"

    for _ in range(5):
        action()

    server = start_dashboard(shield, port=0, open_browser=False, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/finops")
        data = json.loads(resp.read())
        assert data["forecast"]["total_spent"] == 0.25
    finally:
        server.shutdown()


def test_94_dashboard_api_routing():
    shield = _full_shield()
    router = ModelRouter()
    router.add_model("m", cost_per_1k=0.01, max_complexity=1.0)
    router.route("test")

    server = start_dashboard(shield, port=0, open_browser=False, router=router, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/routing")
        data = json.loads(resp.read())
        assert data["enabled"] is True
        assert data["stats"]["total_routed"] == 1
    finally:
        server.shutdown()


def test_95_dashboard_api_alerts():
    shield = _full_shield()
    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=100)
    for _ in range(20):
        detector.record("t", 0.01)
    detector.record("t", 100.0)

    server = start_dashboard(shield, port=0, open_browser=False, anomaly_detector=detector, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/alerts")
        data = json.loads(resp.read())
        assert data["total"] > 0
    finally:
        server.shutdown()


def test_96_dashboard_no_router():
    shield = _full_shield()
    server = start_dashboard(shield, port=0, open_browser=False, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/routing")
        data = json.loads(resp.read())
        assert data["enabled"] is False
    finally:
        server.shutdown()


def test_97_dashboard_html_served():
    shield = _full_shield()
    server = start_dashboard(shield, port=0, open_browser=False, background=True)
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        html = resp.read().decode()
        assert "WarderBox" in html
        assert "FinOps" in html
        assert "Routing" in html
        assert "Alerts" in html
    finally:
        server.shutdown()


def test_98_concurrent_actions():
    """Multiple threads calling shield simultaneously."""
    shield = _full_shield(budget=100.0)

    @shield.tool(cost=0.01)
    def action(n: int) -> int:
        return n

    results = []

    def worker(start):
        for i in range(10):
            r = action(n=start + i)
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i * 10,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 50
    successful = [r for r in results if r.success]
    assert len(successful) == 50


def test_99_end_to_end_scenario():
    """Realistic scenario: agent runs tasks, hits limits, gets scored."""
    pool = AgentPool(default_budget=1.0)
    bot = pool.register("assistant", budget=1.0)
    bot.set_circuit_breaker(CircuitBreaker(failure_threshold=3))
    bot.set_masker(DataMasker())
    bot.set_tracer(Tracer(service_name="e2e"))

    router = ModelRouter()
    router.add_model("haiku", cost_per_1k=0.00025, max_complexity=0.35)
    router.add_model("sonnet", cost_per_1k=0.003, max_complexity=0.65)
    router.add_model("opus", cost_per_1k=0.015, max_complexity=1.0)

    detector = AnomalyDetector(sensitivity=2.0, loop_threshold=50)

    @bot.tool(cost=0.05)
    def search(query: str) -> str:
        router.route(query)
        detector.record("search", 0.05)
        return f"Results for {query}"

    @bot.tool(cost=0.10)
    def send_email(to: str, body: str) -> str:
        router.route("Send email notification")
        detector.record("send_email", 0.10)
        return f"Sent to {to}"

    @bot.tool(cost=0.01)
    def blocked_query(sql: str) -> str:
        return sql

    for i in range(5):
        search(query=f"AI safety topic {i}")
    send_email(to="user@test.com", body="Hello, email with key sk-secret123")
    send_email(to="admin@test.com", body="Report ready")
    blocked_query(sql="DROP TABLE users")

    stats = bot.stats()
    assert stats["total_actions"] == 8
    assert stats["blocked"] == 1
    assert stats["successful"] == 7

    scorer = AgentScorer(bot, agent_name="assistant")
    report = scorer.score()
    assert report.total_actions == 8

    forecaster = CostForecaster(bot)
    forecast = forecaster.forecast()
    assert forecast.total_spent > 0

    assert router.stats().total_routed == 7
    assert detector.stats()["total_records"] == 7


def test_100_version_and_exports():
    """Verify all public exports are accessible."""
    import warderbox
    assert warderbox.__version__ == "0.3.6"

    required = [
        "WarderBox", "AsyncWarderBox", "AgentPool", "AgentScorer",
        "ApprovalManager", "Action", "ActionResult", "CapabilityManager",
        "ChaosEngine", "CircuitBreaker", "CircuitState", "ComplianceReporter",
        "CostForecaster", "FaultType", "PoolScorer", "RiskLevel",
        "ActionLogger", "DataMasker", "RateLimiter", "ToolLimit",
        "RetryPolicy", "Tracer", "EventNotifier",
        "ModelRouter", "ComplexityClassifier", "Complexity",
        "AnomalyDetector", "AnomalyType", "AnomalyAlert",
        "load_policy", "validate_policy", "rules_from_config",
        "TimeoutExecutor", "TimeoutPolicy",
        "IdempotencyGuard", "IdempotencyBackend", "MemoryBackend", "DedupeResult",
        "CostEstimator", "Estimate",
        "OutputValidator", "OutputRule", "OutputAction",
        "FallbackManager", "FallbackRoute",
        "QuotaManager", "QuotaPeriod", "QuotaLimit",
    ]
    for name in required:
        assert hasattr(warderbox, name), f"Missing export: {name}"


# ============================================================
# 101-110: Budget Reservation + Reconciliation + Concurrency
# ============================================================

def test_101_budget_reserve_and_commit():
    """Reserve budget before execution, commit after."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=1.0)
    res = bt.reserve("a1", "tool", 0.30)
    assert res is not None
    assert bt.reserved == 0.30
    assert bt.committed == 0.30
    assert bt.spent == 0.0

    bt.commit(res.reservation_id)
    assert bt.reserved == 0.0
    assert bt.spent == 0.30


def test_102_budget_reserve_blocks_when_full():
    """Second reservation fails when budget is committed."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=0.50)
    r1 = bt.reserve("a1", "tool", 0.30)
    assert r1 is not None
    r2 = bt.reserve("a2", "tool", 0.30)
    assert r2 is None


def test_103_budget_release_frees_reservation():
    """Released reservation frees budget for others."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=0.50)
    r1 = bt.reserve("a1", "tool", 0.30)
    bt.release(r1.reservation_id)
    assert bt.reserved == 0.0
    r2 = bt.reserve("a2", "tool", 0.30)
    assert r2 is not None


def test_104_budget_reconcile_actual_cost():
    """Reconcile with actual provider charges."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=5.0)
    bt.record("a1", "gpt4", 0.10)
    entry = bt.reconcile("a1", 0.15)
    assert entry is not None
    assert entry.reconciled
    assert entry.actual_cost == 0.15
    assert entry.estimated_cost == 0.10
    assert bt.spent == 0.15


def test_105_budget_commit_with_actual_cost():
    """Commit reservation with actual cost different from estimate."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=5.0)
    res = bt.reserve("a1", "search", 0.05)
    bt.commit(res.reservation_id, actual_cost=0.08)
    assert bt.spent == 0.08
    summary = bt.summary()
    assert summary["reconciliation_drift"] == 0.03


def test_106_concurrent_budget_reservations():
    """Multiple threads reserving budget simultaneously."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=1.0)
    results = []

    def worker(i):
        res = bt.reserve(f"a{i}", "tool", 0.15)
        if res:
            time.sleep(0.01)
            bt.commit(res.reservation_id)
            results.append(True)
        else:
            results.append(False)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for r in results if r)
    assert successes <= 6
    assert bt.spent <= 1.0


def test_107_shield_concurrent_budget():
    """Concurrent shield tool calls respect budget via reservations."""
    shield = WarderBox(budget=0.50)
    results = []

    @shield.tool(cost=0.10)
    def action(n: int) -> int:
        return n

    def worker(n):
        r = action(n=n)
        results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r.success]
    blocked = [r for r in results if r.blocked]
    assert len(successes) == 5
    assert len(blocked) == 5
    assert shield._budget.spent == 0.50


def test_108_shield_reconcile_cost():
    """Shield reconcile_cost updates actual charges."""
    shield = _full_shield()

    @shield.tool(cost=0.05)
    def action() -> str:
        return "ok"

    r = action()
    assert r.success
    assert shield._budget.spent == 0.05

    ok = shield.reconcile_cost(r.action.id, 0.08)
    assert ok
    assert shield._budget.spent == 0.08


def test_109_budget_summary_has_reservation_fields():
    """Budget summary includes reservation and reconciliation info."""
    from warderbox.budget import BudgetTracker
    bt = BudgetTracker(budget=5.0)
    res = bt.reserve("a1", "tool", 0.30)
    summary = bt.summary()
    assert "reserved" in summary
    assert "committed" in summary
    assert "active_reservations" in summary
    assert "reconciliation_drift" in summary
    assert summary["reserved"] == 0.30
    assert summary["active_reservations"] == 1
    bt.commit(res.reservation_id)
    summary = bt.summary()
    assert summary["active_reservations"] == 0


def test_110_failed_call_still_costs():
    """Failed calls commit their cost (provider still charges)."""
    shield = _full_shield(budget=5.0)

    @shield.tool(cost=0.10)
    def failing() -> str:
        raise RuntimeError("API error")

    r = failing()
    assert r.error is not None
    assert shield._budget.spent == 0.10
