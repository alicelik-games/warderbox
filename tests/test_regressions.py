"""Regression tests for risk, masking, audit and adapter behavior."""

from __future__ import annotations

import time

from warderbox import WarderBox, load_policy
from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.approval import ApprovalDecision, ApprovalManager
from warderbox.chaos import ChaosEngine, FaultType
from warderbox.integrations.langchain import ShieldedTool
from warderbox.logger import ActionLogger
from warderbox.masking import DataMasker
from warderbox.retry import RetryPolicy
from warderbox.timeout import TimeoutExecutor, TimeoutPolicy


def test_risk_engine_does_not_downgrade_critical():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.01)
    def query_db(sql: str, limit: int = 0) -> str:
        return "EXECUTED"

    for kwargs in (
        {"sql": "DROP TABLE users; UPDATE users SET a=1"},
        {"sql": "DROP TABLE users", "limit": 500},
    ):
        result = query_db(**kwargs)
        assert result.blocked
        assert result.action.risk is RiskLevel.CRITICAL


def test_policy_url_rule_cannot_downgrade_critical():
    shield = load_policy("examples/policy.json")

    @shield.tool(cost=0.01)
    def query_db(sql: str, url: str = "") -> str:
        return "EXECUTED"

    result = query_db(sql="DROP TABLE users", url="https://example.com")
    assert result.blocked
    assert result.action.risk is RiskLevel.CRITICAL


def test_strong_sql_bypasses_are_blocked_but_prose_is_not():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.01)
    def query_db(sql: str) -> str:
        return "EXECUTED"

    @shield.tool(cost=0.01)
    def send_email(body: str) -> str:
        return "SENT"

    for sql in (
        "DROP/**/TABLE users",
        "ALTER TABLE x DROP COLUMN y",
        "DROP SCHEMA public",
        "DROP VIEW v",
        "DELETE FROM data",
        b"DROP TABLE users",
    ):
        result = query_db(sql=sql)
        assert result.blocked, sql

    # Every DELETE FROM is auto-blocked, with or without a WHERE clause.
    result = query_db(sql="DELETE FROM users WHERE id=1")
    assert result.blocked
    assert result.action.risk is RiskLevel.CRITICAL

    for body in ("please delete from my calendar", "please truncate the log"):
        result = send_email(body=body)
        assert not result.blocked
        assert result.action.risk is not RiskLevel.CRITICAL


def test_policy_rules_scan_nested_arguments():
    shield = load_policy("examples/policy.json")

    @shield.tool(cost=0.01)
    def query_db(query: dict) -> str:
        return "EXECUTED"

    result = query_db(query={"sql": "prod-db"})
    assert result.blocked
    assert result.action.risk is RiskLevel.CRITICAL


def test_timeout_retry_does_not_duplicate_execution():
    shield = WarderBox(budget=5.0)
    shield.set_timeout(TimeoutExecutor(TimeoutPolicy(default_seconds=0.05)))
    calls = []

    @shield.tool(
        cost=0.01,
        retry=RetryPolicy(max_retries=2, backoff_base=0.0, jitter=0.0),
    )
    def slow() -> str:
        calls.append(1)
        time.sleep(0.2)
        return "done"

    result = slow()
    assert result.error is not None
    assert len(calls) == 1


def test_modern_keys_and_numeric_timestamp_masking():
    masker = DataMasker()
    for key in (
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "sk-proj-abcdefghijklmnopqrstuvwxyz",
        "sk-abcdefghijklmnopqrstuvwx",
    ):
        assert "REDACTED" in masker.mask_string(key)
    assert masker.mask_string("timestamp 1720000000000") == "timestamp 1720000000000"
    assert masker.mask({"card": 1234567812345678})["card"] != 1234567812345678


def test_audit_log_is_masked_by_default():
    import os
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    try:
        shield = WarderBox(budget=1.0, db_path=path)

        @shield.tool(cost=0.01)
        def send(email: str) -> str:
            return "ok"

        send(email="user@example.com")
        s = shield._logger
        assert s is not None
        s.close()
        logger = ActionLogger(path)
        assert "user@example.com" not in logger.query()[0]["args"]
        logger.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_approval_risk_threshold_applies_to_higher_risks():
    manager = ApprovalManager(default_timeout=0.01)
    manager.require_approval_for_risk("medium", timeout=0.01)
    assert not manager.needs_approval("tool", "LOW")
    assert manager.needs_approval("tool", "MEDIUM")
    assert manager.needs_approval("tool", "HIGH")
    assert manager.needs_approval("tool", "CRITICAL")


def test_approval_auto_deny_false_escalates_instead_of_silent_denial():
    manager = ApprovalManager(default_timeout=0.01)
    manager.require_approval_for_risk("medium", timeout=0.01, auto_deny=False)
    result = manager.request_approval("tool", risk_level="HIGH")
    assert not result.approved
    assert result.decision is ApprovalDecision.ESCALATED


def test_repeated_rollback_undoes_one_action_at_a_time():
    shield = WarderBox(budget=1.0)
    state = {"value": 0}

    @shield.tool(
        cost=0.01,
        snapshot_state=lambda: dict(state),
        restore_state=lambda snap: state.update(snap),
    )
    def increment() -> int:
        state["value"] += 1
        return state["value"]

    increment()
    increment()
    assert state["value"] == 2
    assert shield.rollback()
    assert state["value"] == 1
    assert shield.rollback()
    assert state["value"] == 0
    assert not shield.rollback()


def test_audit_hash_chain_detects_tampering():
    import os
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    try:
        shield = WarderBox(budget=1.0, db_path=path)

        @shield.tool(cost=0.01)
        def ping() -> str:
            return "pong"

        ping()
        logger = ActionLogger(path)
        assert logger.verify_integrity()
        logger._conn.execute("UPDATE actions SET output = 'TAMPERED' WHERE rowid = 1")
        logger._conn.commit()
        assert not logger.verify_integrity()
        logger.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_adapter_does_not_reveal_the_block_reason_to_the_model():
    shield = WarderBox(budget=1.0)
    tool = ShieldedTool(
        shield=shield,
        name="run_sql",
        func=lambda sql: "ran",
        cost=0.01,
    )
    output = tool.run("DROP TABLE users")
    assert output == "[BLOCKED] Action blocked by WarderBox safety policy"
    assert "Destructive SQL" not in output
    assert "Destructive SQL" in shield.history(raw=True)[-1]["block_reason"]


def test_chaos_production_switch():
    shield = WarderBox(budget=1.0)
    chaos = ChaosEngine(allow_production=False)
    chaos.add_fault("ping", FaultType.ERROR, probability=1.0)
    shield.set_chaos(chaos)

    @shield.tool(cost=0.01)
    def ping() -> str:
        return "pong"

    assert ping().error is None

    shield2 = WarderBox(budget=1.0)
    chaos2 = ChaosEngine(allow_production=True)
    chaos2.add_fault("ping2", FaultType.ERROR, probability=1.0)
    shield2.set_chaos(chaos2)

    @shield2.tool(cost=0.01)
    def ping2() -> str:
        return "pong"

    assert ping2().error is not None


def test_public_execute_entrypoint():
    shield = WarderBox(budget=1.0)
    result = shield.execute(Action(tool_name="ping"), lambda: "pong", cost=0.01)
    assert isinstance(result, ActionResult)
    assert result.output == "pong"


def test_webhook_dns_resolution_blocks_private_hosts(monkeypatch):
    import socket

    from warderbox.webhooks import _is_private_host

    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _is_private_host("internal.example", resolve=True)


def test_actual_cost_callback_reconciles_budget():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=1.0, actual_cost=lambda output: 0.1)
    def call() -> str:
        return "ok"

    result = call()
    assert result.cost == 0.1
    assert shield.budget_summary["spent"] == 0.1
    assert shield.history()[-1]["cost"] == 0.1


def test_shield_approval_threshold_covers_high_risk():
    shield = WarderBox(budget=5.0)
    manager = ApprovalManager(default_timeout=0.01)
    manager.require_approval_for_risk("medium", timeout=0.01)
    shield.set_approval(manager)

    @shield.tool(cost=0.01)
    def send_email(to: str) -> str:
        return "sent"

    result = send_email(to="a@b.com")
    assert result.blocked
    # send_email is MEDIUM by default, so the medium threshold applies.
    assert result.action.risk is RiskLevel.MEDIUM


def test_verify_audit_public_api():
    import os
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    try:
        shield = WarderBox(budget=1.0, db_path=path)

        @shield.tool(cost=0.01)
        def ping() -> str:
            return "pong"

        ping()
        assert shield.verify_audit()
        shield._logger._conn.execute(
            "UPDATE actions SET output = 'TAMPERED' WHERE rowid = 1"
        )
        shield._logger._conn.commit()
        assert not shield.verify_audit()
        shield._logger.close()
    finally:
        if os.path.exists(path):
            os.remove(path)
