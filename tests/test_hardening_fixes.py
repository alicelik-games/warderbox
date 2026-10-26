"""P2 hardening regression tests: history cap, policy validation, db side effects."""

from __future__ import annotations

import json

import pytest

from warderbox import WarderBox
from warderbox.policy import load_policy, validate_policy


def test_p2_2_history_is_capped():
    shield = WarderBox(budget=None, max_history=10)

    @shield.tool(cost=0.0)
    def noop(i: int) -> int:
        return i

    for i in range(50):
        noop(i=i)

    assert len(shield.history()) == 10


def test_p2_4_from_config_no_implicit_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shield = WarderBox.from_config({"budget": 1.0})
    assert shield._logger is None
    # No stray warderbox.db written to the working directory.
    assert not (tmp_path / "warderbox.db").exists()


def test_p2_4_load_policy_rejects_invalid(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mode": "banana"}))
    with pytest.raises(ValueError):
        load_policy(str(bad))


def test_p2_4_validate_policy_reports_issues(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"budget": True, "mode": "nope"}))
    issues = validate_policy(str(bad))
    assert any("budget" in i for i in issues)
    assert any("mode" in i for i in issues)


# --- P2 remaining: webhook SSRF + masking, budget O(1), span cap, quota persist, pool override ---

def test_webhook_rejects_private_host():
    from warderbox.webhooks import EventNotifier
    n = EventNotifier()
    with pytest.raises(ValueError):
        n.add_webhook("http://127.0.0.1:8080/hook")
    with pytest.raises(ValueError):
        n.add_webhook("http://localhost/hook")
    with pytest.raises(ValueError):
        n.add_webhook("ftp://example.com/x")
    # public host is accepted
    n.add_webhook("https://example.com/hook")
    assert len(n._webhooks) == 1


def test_webhook_allow_private_override():
    from warderbox.webhooks import EventNotifier
    n = EventNotifier(allow_private_hosts=True)
    n.add_webhook("http://127.0.0.1:9000/hook")
    assert len(n._webhooks) == 1


def test_budget_spent_matches_entries():
    shield = WarderBox(budget=None)

    @shield.tool(cost=0.03)
    def work(i: int) -> int:
        return i

    for i in range(20):
        work(i=i)
    assert abs(shield.budget_summary["spent"] - 0.6) < 1e-9


def test_tracer_spans_capped():
    from warderbox.tracing import Tracer
    tr = Tracer(max_spans=5)
    for i in range(20):
        s = tr.start_span(f"s{i}")
        tr.end_span(s)
    assert len(tr.spans()) == 5


def test_quota_persists_across_restart(tmp_path):
    from warderbox.quota import QuotaManager, QuotaPeriod
    path = str(tmp_path / "quota.json")
    qm = QuotaManager(persist_path=path)
    qm.set_quota("send_email", QuotaPeriod.MONTHLY, 100)
    qm.record("send_email")
    qm.record("send_email")

    reborn = QuotaManager(persist_path=path)
    usage = reborn.get_usage("send_email")
    assert usage[0]["current_calls"] == 2


def test_pool_register_explicit_overrides_config():
    from warderbox.multiagent import AgentPool
    pool = AgentPool()
    shield = pool.register("x", budget=3.0, config={"budget": 99.0, "mode": "production"})
    assert shield._budget.budget == 3.0
