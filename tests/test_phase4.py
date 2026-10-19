"""Tests for Phase 4: policy files, retry, CrewAI, dashboard."""

import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, "src")

from warderbox import WarderBox, RetryPolicy, load_policy, validate_policy
from warderbox.retry import execute_with_retry
from warderbox.integrations.crewai import ShieldedCrewTool, shield_crewai_tools
from warderbox.dashboard import start_dashboard


# -- Policy-as-code --

def test_load_policy_json():
    policy = {
        "budget": 5.0,
        "mode": "production",
        "rules": [
            {
                "name": "no_secrets",
                "match": {"args_contain": "secret"},
                "risk": "critical",
                "reason": "Secret access blocked",
            }
        ],
    }

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(policy, f)
        path = f.name

    try:
        shield = load_policy(path)

        @shield.tool(cost=0.01)
        def search(query: str):
            return "results"

        safe = search(query="find user by name")
        assert not safe.blocked

        blocked = search(query="find secret key")
        assert blocked.blocked
    finally:
        os.unlink(path)


def test_validate_policy_valid():
    policy = {"budget": 10.0, "mode": "production", "rules": []}

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(policy, f)
        path = f.name

    try:
        issues = validate_policy(path)
        assert issues == []
    finally:
        os.unlink(path)


def test_validate_policy_invalid():
    policy = {
        "budget": "not_a_number",
        "mode": "invalid_mode",
        "rules": [{"risk": "super_high"}],
    }

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(policy, f)
        path = f.name

    try:
        issues = validate_policy(path)
        assert len(issues) >= 2
        assert any("budget" in i for i in issues)
        assert any("mode" in i for i in issues)
    finally:
        os.unlink(path)


# -- Retry --

def test_retry_succeeds_after_failures():
    call_count = 0

    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("timeout")
        return "success"

    policy = RetryPolicy(max_retries=5, backoff_base=0.01, backoff_max=0.05, jitter=0)
    result, errors = execute_with_retry(flaky, (), {}, policy)
    assert result == "success"
    assert len(errors) == 2
    assert call_count == 3


def test_retry_exhausted():
    def always_fail():
        raise ValueError("permanent error")

    policy = RetryPolicy(max_retries=2, backoff_base=0.01, backoff_max=0.05, jitter=0)
    try:
        execute_with_retry(always_fail, (), {}, policy)
        assert False, "Should have raised"
    except ValueError as e:
        assert "permanent error" in str(e)


def test_retry_conditional():
    call_count = 0

    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("retry this")
        if call_count == 2:
            raise ValueError("don't retry this")
        return "ok"

    policy = RetryPolicy(
        max_retries=5,
        backoff_base=0.01,
        jitter=0,
        retry_on=lambda e: isinstance(e, ConnectionError),
    )
    try:
        execute_with_retry(flaky, (), {}, policy)
        assert False, "Should have raised ValueError"
    except ValueError:
        assert call_count == 2


def test_retry_in_shield():
    call_count = 0

    shield = WarderBox(budget=10.0)
    policy = RetryPolicy(max_retries=3, backoff_base=0.01, jitter=0)

    @shield.tool(cost=0.1, retry=policy)
    def flaky_tool(msg: str):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("fail")
        return f"ok: {msg}"

    result = flaky_tool(msg="hello")
    assert not result.blocked
    assert result.output == "ok: hello"
    assert call_count == 3


# -- CrewAI --

def test_crewai_shielded_tool():
    shield = WarderBox(budget=5.0)

    tool = ShieldedCrewTool(
        shield=shield,
        name="web_search",
        description="Search the web",
        func=lambda query: f"results for {query}",
        cost=0.05,
    )

    result = tool.run(query="AI safety")
    assert "results for AI safety" in result

    result2 = tool.run(query="DROP TABLE users")
    assert "BLOCKED" in result2


def test_crewai_shield_existing_tools():
    shield = WarderBox(budget=5.0)

    class FakeCrewTool:
        def __init__(self, name, description):
            self.name = name
            self.description = description
        def _run(self, **kwargs):
            return f"executed {self.name}"

    tools = [
        FakeCrewTool("search", "Web search"),
        FakeCrewTool("calculator", "Math operations"),
    ]

    shielded = shield_crewai_tools(shield, tools, cost_map={"search": 0.05})
    assert len(shielded) == 2
    assert shielded[0].name == "search"
    result = shielded[1](input="2+2")
    assert "executed calculator" in result


# -- Dashboard --

def test_dashboard_serves():
    shield = WarderBox(budget=10.0)

    @shield.tool(cost=0.01)
    def ping(msg: str):
        return "pong"

    ping(msg="test")
    ping(msg="DROP TABLE x")

    server = start_dashboard(shield, port=0, open_browser=False, background=True)
    port = server.server_address[1]

    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        html = resp.read().decode()
        assert "WarderBox" in html

        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats")
        stats = json.loads(resp.read())
        assert stats["total_actions"] == 2
        assert stats["blocked"] == 1

        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/history")
        history = json.loads(resp.read())
        assert len(history) == 2
    finally:
        server.shutdown()


if __name__ == "__main__":
    test_load_policy_json()
    test_validate_policy_valid()
    test_validate_policy_invalid()
    test_retry_succeeds_after_failures()
    test_retry_exhausted()
    test_retry_conditional()
    test_retry_in_shield()
    test_crewai_shielded_tool()
    test_crewai_shield_existing_tools()
    test_dashboard_serves()
    print("All Phase 4 tests passed!")
