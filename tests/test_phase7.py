"""Tests for Phase 7: circuit breaker, approval workflows, capabilities, compliance."""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, "src")

from warderbox import (
    ApprovalManager,
    CapabilityManager,
    CircuitBreaker,
    CircuitState,
    ComplianceReporter,
    WarderBox,
)
from warderbox.approval import ApprovalDecision

# -- Circuit Breaker --

def test_circuit_breaker_stays_closed_on_success():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_success("search")
    cb.record_success("search")
    assert cb.get_state("search") == CircuitState.CLOSED
    assert not cb.is_open("search")


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("search")
    cb.record_failure("search")
    assert cb.get_state("search") == CircuitState.CLOSED
    cb.record_failure("search")
    assert cb.get_state("search") == CircuitState.OPEN
    assert cb.is_open("search")


def test_circuit_breaker_should_allow():
    cb = CircuitBreaker(failure_threshold=2)
    allowed, _ = cb.should_allow("api")
    assert allowed

    cb.record_failure("api")
    cb.record_failure("api")
    allowed, reason = cb.should_allow("api")
    assert not allowed
    assert "open" in reason.lower()


def test_circuit_breaker_half_open():
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.1, success_threshold=1)
    cb.record_failure("api")
    cb.record_failure("api")
    assert cb.get_state("api") == CircuitState.OPEN

    time.sleep(0.15)
    assert cb.get_state("api") == CircuitState.HALF_OPEN

    allowed, _ = cb.should_allow("api")
    assert allowed

    cb.record_success("api")
    assert cb.get_state("api") == CircuitState.CLOSED


def test_circuit_breaker_half_open_failure_reopens():
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.1)
    cb.record_failure("api")
    cb.record_failure("api")
    time.sleep(0.15)

    cb.should_allow("api")
    cb.record_failure("api")
    assert cb.get_state("api") == CircuitState.OPEN


def test_circuit_breaker_reset():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure("api")
    cb.record_failure("api")
    assert cb.is_open("api")
    cb.reset("api")
    assert not cb.is_open("api")


def test_circuit_breaker_callbacks():
    opened = []
    closed = []
    cb = CircuitBreaker(
        failure_threshold=2,
        reset_timeout=0.1,
        success_threshold=1,
        on_open=lambda t: opened.append(t),
        on_close=lambda t: closed.append(t),
    )
    cb.record_failure("x")
    cb.record_failure("x")
    assert opened == ["x"]

    time.sleep(0.15)
    cb.should_allow("x")
    cb.record_success("x")
    assert closed == ["x"]


def test_circuit_breaker_in_shield():
    cb = CircuitBreaker(failure_threshold=2)
    shield = WarderBox(budget=10.0)
    shield.set_circuit_breaker(cb)

    call_count = 0

    @shield.tool(cost=0.01)
    def flaky(msg: str):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("fail")

    flaky(msg="a")
    flaky(msg="b")

    result = flaky(msg="c")
    assert result.blocked
    assert "circuit breaker" in result.block_reason.lower()
    assert call_count == 2


def test_circuit_breaker_stats():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("a")
    cb.record_failure("a")
    cb.record_success("b")
    s = cb.stats()
    assert s["a"]["failure_count"] == 2
    assert s["a"]["state"] == "closed"
    assert s["b"]["state"] == "closed"


# -- Approval Workflows --

def test_approval_not_required():
    mgr = ApprovalManager()
    result = mgr.request_approval("search")
    assert result.approved
    assert result.decision == ApprovalDecision.NOT_REQUIRED


def test_approval_timeout_auto_deny():
    mgr = ApprovalManager(default_timeout=0.1)
    mgr.require_approval("send_email", timeout=0.1)

    result = mgr.request_approval("send_email")
    assert not result.approved
    assert result.decision == ApprovalDecision.TIMEOUT


def test_approval_approve_via_callback():
    requests_received = []

    def on_request(**kwargs):
        requests_received.append(kwargs)

    mgr = ApprovalManager(on_approval_request=on_request)
    mgr.require_approval("deploy", timeout=2.0)

    def approve_after_delay():
        time.sleep(0.1)
        if requests_received:
            mgr.approve(requests_received[0]["request_id"], approver="admin")

    t = threading.Thread(target=approve_after_delay)
    t.start()

    result = mgr.request_approval("deploy", args={"env": "staging"})
    t.join()

    assert result.approved
    assert result.decision == ApprovalDecision.APPROVED


def test_approval_deny_via_callback():
    requests_received = []

    def on_request(**kwargs):
        requests_received.append(kwargs)

    mgr = ApprovalManager(on_approval_request=on_request)
    mgr.require_approval("delete_all", timeout=2.0)

    def deny_after_delay():
        time.sleep(0.1)
        if requests_received:
            mgr.deny(requests_received[0]["request_id"], reason="too risky")

    t = threading.Thread(target=deny_after_delay)
    t.start()

    result = mgr.request_approval("delete_all")
    t.join()

    assert not result.approved
    assert result.decision == ApprovalDecision.DENIED


def test_approval_stats():
    mgr = ApprovalManager(default_timeout=0.05)
    mgr.require_approval("x", timeout=0.05)
    mgr.require_approval("y", timeout=0.05)

    mgr.request_approval("x")
    mgr.request_approval("y")

    s = mgr.stats()
    assert s["total_requests"] == 2
    assert s["timed_out"] == 2


def test_approval_risk_based():
    mgr = ApprovalManager(default_timeout=0.05)
    mgr.require_approval_for_risk("HIGH", timeout=0.05)

    assert mgr.needs_approval("any_tool", risk_level="HIGH")
    assert not mgr.needs_approval("any_tool", risk_level="LOW")


# -- Capabilities --

def test_capabilities_allow_list():
    caps = CapabilityManager()
    caps.grant("researcher", allow=["search", "read_file"])

    ok, _ = caps.check("researcher", "search")
    assert ok

    ok, reason = caps.check("researcher", "delete_file")
    assert not ok
    assert "not allowed" in reason


def test_capabilities_deny_list():
    caps = CapabilityManager()
    caps.grant("intern", deny=["delete_record", "send_email"])

    ok, _ = caps.check("intern", "search")
    assert ok

    ok, reason = caps.check("intern", "delete_record")
    assert not ok
    assert "denied" in reason


def test_capabilities_args_constraint():
    caps = CapabilityManager()
    caps.add_constraint("coder", "run_sql", args_deny=["DROP", "TRUNCATE"])

    ok, _ = caps.check("coder", "run_sql", args={"sql": "SELECT 1"})
    assert ok

    ok, reason = caps.check("coder", "run_sql", args={"sql": "DROP TABLE users"})
    assert not ok
    assert "denied term" in reason


def test_capabilities_max_cost():
    caps = CapabilityManager()
    caps.add_constraint("budget_agent", "expensive_call", max_cost=1.0)

    ok, _ = caps.check("budget_agent", "expensive_call", cost=0.5)
    assert ok

    ok, reason = caps.check("budget_agent", "expensive_call", cost=2.0)
    assert not ok
    assert "exceeds max" in reason.lower()


def test_capabilities_unknown_agent():
    # Secure default: an agent with no granted capabilities is denied.
    caps = CapabilityManager()
    ok, _ = caps.check("unknown_agent", "any_tool")
    assert not ok

    # Opt-out for permissive setups.
    permissive = CapabilityManager(fail_closed=False)
    ok2, _ = permissive.check("unknown_agent", "any_tool")
    assert ok2


def test_capabilities_revoke():
    caps = CapabilityManager()
    caps.grant("bot", allow=["search", "write"])
    caps.revoke("bot", tools=["write"])

    ok, _ = caps.check("bot", "search")
    assert ok

    ok, _ = caps.check("bot", "write")
    assert not ok


def test_capabilities_in_shield():
    caps = CapabilityManager()
    caps.grant("test_agent", deny=["dangerous_tool"])

    shield = WarderBox(budget=10.0)
    shield.set_capabilities(caps)
    shield._agent_name = "test_agent"

    @shield.tool(cost=0.01)
    def dangerous_tool(x: str):
        return x

    result = dangerous_tool(x="hello")
    assert result.blocked
    assert "capability denied" in result.block_reason.lower()


def test_capabilities_stats():
    caps = CapabilityManager()
    caps.grant("bot", allow=["a", "b"], deny=["c"])
    caps.check("bot", "a")
    caps.check("bot", "c")
    s = caps.stats()
    assert s["bot"]["total_checks"] == 2
    assert s["bot"]["denied_checks"] == 1


# -- Compliance Reports --

def test_compliance_basic_report():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.05)
        def search(q: str):
            return f"results for {q}"

        @shield.tool(cost=0.01)
        def danger(q: str):
            return q

        search(q="hello")
        search(q="world")
        danger(q="DROP TABLE users")

        shield._logger.close()

        reporter = ComplianceReporter(db_path)
        report = reporter.generate_report()

        assert report.total_actions == 3
        assert report.total_cost > 0
        assert report.successful_actions >= 1
        assert len(report.tool_usage) >= 1
        assert report.summary
    finally:
        os.unlink(db_path)


def test_compliance_export_json():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        json_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def ping(msg: str):
            return "pong"

        ping(msg="test")
        shield._logger.close()

        reporter = ComplianceReporter(db_path)
        reporter.export_json(json_path)

        with open(json_path) as f:
            data = json.load(f)

        assert data["total_actions"] == 1
        assert "generated_at" in data
        assert "risk_breakdown" in data
    finally:
        os.unlink(db_path)
        os.unlink(json_path)


def test_compliance_html_report():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def task(n: str):
            return n

        task(n="a")
        task(n="b")
        shield._logger.close()

        reporter = ComplianceReporter(db_path)
        html = reporter.to_html()
        assert "WarderBox Audit Report" in html
        assert "Total Actions" in html
    finally:
        os.unlink(db_path)


def test_compliance_anomaly_detection():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=100.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def action(sql: str):
            return sql

        for _ in range(5):
            action(sql="DROP TABLE x")

        shield._logger.close()

        reporter = ComplianceReporter(db_path)
        report = reporter.generate_report()

        has_critical = any("CRITICAL" in a for a in report.anomalies)
        assert has_critical or report.blocked_actions > 0
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    test_circuit_breaker_stays_closed_on_success()
    test_circuit_breaker_opens_after_threshold()
    test_circuit_breaker_should_allow()
    test_circuit_breaker_half_open()
    test_circuit_breaker_half_open_failure_reopens()
    test_circuit_breaker_reset()
    test_circuit_breaker_callbacks()
    test_circuit_breaker_in_shield()
    test_circuit_breaker_stats()
    test_approval_not_required()
    test_approval_timeout_auto_deny()
    test_approval_approve_via_callback()
    test_approval_deny_via_callback()
    test_approval_stats()
    test_approval_risk_based()
    test_capabilities_allow_list()
    test_capabilities_deny_list()
    test_capabilities_args_constraint()
    test_capabilities_max_cost()
    test_capabilities_unknown_agent()
    test_capabilities_revoke()
    test_capabilities_in_shield()
    test_capabilities_stats()
    test_compliance_basic_report()
    test_compliance_export_json()
    test_compliance_html_report()
    test_compliance_anomaly_detection()
    print("All Phase 7 tests passed!")
