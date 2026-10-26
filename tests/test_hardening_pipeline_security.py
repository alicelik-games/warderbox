"""Hardening regression tests.

Each test encodes a security/correctness guarantee that the audit proved
was violated. They must fail against the pre-fix code and pass after.
"""

from __future__ import annotations

import threading
import time

import pytest

from warderbox import (
    ApprovalManager,
    CapabilityManager,
    CircuitBreaker,
    DataMasker,
    FallbackManager,
    IdempotencyGuard,
    RetryPolicy,
    TimeoutExecutor,
    TimeoutPolicy,
    WarderBox,
)
from warderbox.logger import ActionLogger
from warderbox.multiagent import AgentPool

# ---------------------------------------------------------------------------
# P0-1  Positional / nested arguments must not bypass content rules
# ---------------------------------------------------------------------------

def test_p0_1_positional_destructive_sql_blocked():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.01)
    def query_db(sql: str) -> str:
        return "EXECUTED"

    result = query_db("DROP TABLE users")
    assert result.blocked is True
    assert result.output != "EXECUTED"


def test_p0_1_keyword_destructive_sql_still_blocked():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.01)
    def query_db(sql: str) -> str:
        return "EXECUTED"

    assert query_db(sql="DROP TABLE users").blocked is True


def test_p0_1_nested_destructive_sql_blocked():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.01)
    def run(payload: dict) -> str:
        return "EXECUTED"

    assert run(payload={"sql": "DROP TABLE users"}).blocked is True


# ---------------------------------------------------------------------------
# P0-2  Approval must actually gate execution
# ---------------------------------------------------------------------------

def test_p0_2_approval_denied_blocks_execution():
    approver = ApprovalManager(default_timeout=0.2)
    approver.require_approval("send_email", auto_deny=True)

    shield = WarderBox(budget=5.0)
    shield.set_approval(approver)

    sent = []

    @shield.tool(cost=0.01)
    def send_email(to: str) -> str:
        sent.append(to)
        return "SENT"

    result = send_email(to="x@y.com")
    assert result.blocked is True
    assert sent == []


def test_p0_2_approval_granted_allows_execution():
    def auto_grant(**kwargs):
        return True

    approver = ApprovalManager(default_timeout=5.0, on_approval_request=lambda **k: None)
    approver.require_approval("send_email")

    shield = WarderBox(budget=5.0)
    shield.set_approval(approver)

    @shield.tool(cost=0.01)
    def send_email(to: str) -> str:
        return "SENT"

    # Approve in a background thread the moment the gate opens.
    def approve():
        time.sleep(0.05)
        for _ in range(50):
            pending = approver.pending_requests()
            if pending:
                approver.approve(pending[0])
                return
            time.sleep(0.02)

    t = threading.Thread(target=approve)
    t.start()
    result = send_email(to="x@y.com")
    t.join()
    assert result.success is True
    assert result.output == "SENT"


# ---------------------------------------------------------------------------
# P0-3  Agent capabilities must be enforced via AgentPool (fail-closed)
# ---------------------------------------------------------------------------

def test_p0_3_capability_enforced_through_pool():
    caps = CapabilityManager()
    caps.grant("researcher", deny=["delete_record"])

    pool = AgentPool(default_budget=5.0)
    shield = pool.register("researcher")
    shield.set_capabilities(caps)

    @shield.tool(cost=0.01)
    def delete_record(id: int) -> str:
        return "DELETED"

    result = delete_record(id=1)
    assert result.blocked is True


def test_p0_3_unknown_agent_fails_closed_when_allowlist_used():
    caps = CapabilityManager()
    caps.grant("known", allow=["safe_tool"])

    shield = WarderBox(budget=5.0, agent_name="ghost")
    shield.set_capabilities(caps)

    @shield.tool(cost=0.01)
    def safe_tool() -> str:
        return "OK"

    assert safe_tool().blocked is True


# ---------------------------------------------------------------------------
# P0-4  Masking must cover output, not just args
# ---------------------------------------------------------------------------

def test_p0_4_output_masked_in_log(tmp_path):
    db = str(tmp_path / "audit.db")
    shield = WarderBox(budget=5.0, db_path=db)
    shield.set_masker(DataMasker())

    @shield.tool(cost=0.01)
    def leak() -> str:
        return "ssn 123-45-6789 key sk-abcdefghijklmnopqrstuvwx"

    leak()
    rows = ActionLogger(db).query(limit=1)
    assert "123-45-6789" not in (rows[0]["output"] or "")


# ---------------------------------------------------------------------------
# P0-5  Fallback must respect budget / protections
# ---------------------------------------------------------------------------

def test_p0_5_fallback_respects_budget():
    shield = WarderBox(budget=0.0)
    cb = CircuitBreaker(failure_threshold=1)
    shield.set_circuit_breaker(cb)

    fb = FallbackManager()
    fb.register("primary", "secondary",
                fallback_fn=lambda **kw: "fallback-ran",
                fallback_cost=100.0)
    shield.set_fallback(fb)

    @shield.tool(cost=0.01)
    def primary() -> str:
        return "primary"

    cb.record_failure("primary")
    result = primary()
    assert result.blocked is True
    assert shield.budget_summary["spent"] <= 0.0


# ---------------------------------------------------------------------------
# P0-6  Retry count correct + idempotency honored across retries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("max_retries,expected", [(0, 1), (1, 2), (2, 3)])
def test_p0_6_retry_attempt_count(max_retries, expected):
    from warderbox.retry import execute_with_retry

    attempts = []

    def f():
        attempts.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        execute_with_retry(f, (), {},
                           RetryPolicy(max_retries=max_retries, backoff_base=0, jitter=0))
    assert len(attempts) == expected


def test_p0_6_idempotent_tool_not_double_sent_on_retry():
    guard = IdempotencyGuard()
    guard.add_protected_tool("send_email")

    shield = WarderBox(budget=5.0)
    shield.set_idempotency(guard)

    sent = []

    @shield.tool(cost=0.01,
                 retry=RetryPolicy(max_retries=2, backoff_base=0, jitter=0))
    def send_email(to: str) -> str:
        sent.append(to)
        if len(sent) == 1:
            raise RuntimeError("provider committed but response timed out")
        return "sent"

    send_email(to="a@b.c")
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# P0-7  Timeout must apply even when retry is configured
# ---------------------------------------------------------------------------

def test_p0_7_timeout_applies_with_retry():
    shield = WarderBox(budget=5.0)
    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=0.05)))

    @shield.tool(cost=0.01,
                 retry=RetryPolicy(max_retries=1, backoff_base=0, jitter=0))
    def slow() -> str:
        time.sleep(0.5)
        return "done"

    result = slow()
    assert result.success is False
    assert result.output != "done"


# ---------------------------------------------------------------------------
# P0-8  Test mode must not consume real budget / rate
# ---------------------------------------------------------------------------

def test_p0_8_test_mode_does_not_consume_budget():
    shield = WarderBox(budget=1.0, mode="test")

    @shield.tool(cost=0.6)
    def act() -> str:
        return "REAL"

    r1 = act()
    r2 = act()
    assert r1.blocked is False
    assert r2.blocked is False
    assert shield.budget_summary["spent"] == 0.0


# ---------------------------------------------------------------------------
# P1-6  Logger must be usable from worker threads
# ---------------------------------------------------------------------------

def test_p1_6_tool_call_from_worker_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = str(tmp_path / "threaded.db")
    shield = WarderBox(budget=100.0, db_path=db)

    @shield.tool(cost=0.01)
    def work(n: int) -> int:
        return n * 2

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda i: work(n=i), range(40)))

    assert all(r.success for r in results)
    assert len(ActionLogger(db).query(limit=100)) == 40
