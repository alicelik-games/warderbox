"""Tests for WarderBox core functionality."""

import sys
sys.path.insert(0, "src")

from warderbox import WarderBox, RiskLevel


def make_shield(**kwargs):
    return WarderBox(budget=10.0, auto_snapshot=True, **kwargs)


# -- Blocking --

def test_blocks_destructive_sql():
    shield = make_shield()
    db = {"data": [1, 2, 3]}

    @shield.tool(cost=0.01, snapshot_state=lambda: dict(db), restore_state=lambda s: db.update(s))
    def run_sql(query: str):
        return "ok"

    result = run_sql(query="DROP TABLE users")
    assert result.blocked
    assert "CRITICAL" in result.block_reason


def test_blocks_truncate():
    shield = make_shield()

    @shield.tool(cost=0.01)
    def run_sql(query: str):
        return "ok"

    result = run_sql(query="TRUNCATE TABLE orders")
    assert result.blocked


def test_allows_safe_query():
    shield = make_shield()

    @shield.tool(cost=0.01)
    def run_sql(query: str):
        return "ok"

    result = run_sql(query="SELECT * FROM users")
    assert not result.blocked
    assert result.output == "ok"


# -- Budget --

def test_budget_tracking():
    shield = WarderBox(budget=0.05)

    @shield.tool(cost=0.02)
    def cheap_call():
        return "done"

    r1 = cheap_call()
    assert r1.success
    assert shield.budget_summary["spent"] == 0.02

    r2 = cheap_call()
    assert r2.success
    assert shield.budget_summary["spent"] == 0.04

    r3 = cheap_call()
    assert r3.blocked
    assert "Budget exceeded" in r3.block_reason


# -- Rollback --

def test_rollback():
    shield = make_shield()
    state = {"count": 5}

    @shield.tool(
        cost=0.01,
        snapshot_state=lambda: dict(state),
        restore_state=lambda s: state.update(s),
    )
    def increment():
        state["count"] += 1
        return state["count"]

    result = increment()
    assert result.output == 6
    assert state["count"] == 6

    success = shield.rollback()
    assert success
    assert state["count"] == 5


# -- Test mode --

def test_test_mode_no_side_effects():
    shield = WarderBox(budget=10.0, mode="test")
    called = {"value": False}

    @shield.tool(cost=0.01)
    def dangerous_action():
        called["value"] = True
        return "executed"

    result = dangerous_action()
    assert not called["value"]
    assert "TEST MODE" in result.output


# -- History --

def test_history():
    shield = make_shield()

    @shield.tool(cost=0.05)
    def do_something():
        return "done"

    do_something()
    do_something()

    history = shield.history()
    assert len(history) == 2
    assert all(h["tool"] == "do_something" for h in history)


if __name__ == "__main__":
    test_blocks_destructive_sql()
    test_blocks_truncate()
    test_allows_safe_query()
    test_budget_tracking()
    test_rollback()
    test_test_mode_no_side_effects()
    test_history()
    print("All tests passed!")
