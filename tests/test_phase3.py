"""Tests for Phase 3: rate limiting, webhooks, async, and Anthropic integration."""

import asyncio
import sys

sys.path.insert(0, "src")

from warderbox import AsyncWarderBox, EventNotifier, ToolLimit, WarderBox
from warderbox.integrations.anthropic_sdk import AnthropicShieldMiddleware

# -- Rate Limiting --

def test_rate_limiter_global_rpm():
    shield = WarderBox(budget=100.0, global_rpm=3)

    @shield.tool(cost=0.01)
    def ping(msg: str):
        return "pong"

    r1 = ping(msg="a")
    r2 = ping(msg="b")
    r3 = ping(msg="c")
    assert not r1.blocked
    assert not r2.blocked
    assert not r3.blocked

    r4 = ping(msg="d")
    assert r4.blocked
    assert "rate limit" in r4.block_reason.lower()


def test_rate_limiter_per_tool():
    shield = WarderBox(
        budget=100.0,
        tool_limits={"send_email": ToolLimit(rpm=2)},
    )

    @shield.tool(cost=0.01)
    def send_email(to: str):
        return f"sent to {to}"

    @shield.tool(cost=0.01)
    def read_file(path: str):
        return "contents"

    send_email(to="a@b.com")
    send_email(to="c@d.com")
    r3 = send_email(to="e@f.com")
    assert r3.blocked
    assert "send_email" in r3.block_reason

    r_file = read_file(path="/tmp/x")
    assert not r_file.blocked


# -- Webhooks / Event Notifier --

def test_event_notifier_callbacks():
    events_received = []

    notifier = EventNotifier()
    notifier.on("action_blocked", lambda event, data: events_received.append(data))

    notifier.emit("action_blocked", {"tool": "run_sql", "reason": "DROP TABLE"})
    notifier.emit("action_completed", {"tool": "read_file"})

    assert len(events_received) == 1
    assert events_received[0]["tool"] == "run_sql"


def test_event_notifier_wildcard():
    all_events = []

    notifier = EventNotifier()
    notifier.on("*", lambda event, data: all_events.append(event))

    notifier.emit("action_blocked", {})
    notifier.emit("action_completed", {})
    notifier.emit("budget_warning", {})

    assert len(all_events) == 3


def test_shield_emits_events():
    events = []

    shield = WarderBox(budget=10.0)
    shield.notifier.on("action_blocked", lambda e, d: events.append(("blocked", d)))
    shield.notifier.on("action_completed", lambda e, d: events.append(("completed", d)))

    @shield.tool(cost=0.01)
    def safe_query(sql: str):
        return "ok"

    @shield.tool(cost=0.01)
    def danger_query(sql: str):
        return "ok"

    safe_query(sql="SELECT 1")
    danger_query(sql="DROP TABLE users")

    blocked_events = [e for e in events if e[0] == "blocked"]
    completed_events = [e for e in events if e[0] == "completed"]
    assert len(blocked_events) == 1
    assert len(completed_events) == 1


# -- Anthropic SDK Integration --

def test_anthropic_middleware():
    shield = WarderBox(budget=5.0)
    middleware = AnthropicShieldMiddleware(
        shield=shield,
        executors={"search": lambda query: f"results for {query}"},
        cost_map={"search": 0.05},
    )

    result = middleware.handle_tool_use({
        "name": "search",
        "input": {"query": "AI safety"},
        "id": "tool_123",
    })
    assert not result.blocked
    assert "results for AI safety" in str(result.output)

    result2 = middleware.handle_tool_use({
        "name": "unknown_tool",
        "input": {},
        "id": "tool_456",
    })
    assert result2.blocked
    assert "No executor" in result2.block_reason


def test_anthropic_format_tool_result():
    shield = WarderBox(budget=5.0)
    middleware = AnthropicShieldMiddleware(
        shield=shield,
        executors={"run_sql": lambda query: f"executed: {query}"},
        cost_map={"run_sql": 0.02},
    )

    result = middleware.handle_tool_use({
        "name": "run_sql",
        "input": {"query": "DROP TABLE users"},
        "id": "tool_789",
    })
    formatted = middleware.format_tool_result(result)
    assert formatted["type"] == "tool_result"
    assert formatted["is_error"] is True
    assert "BLOCKED" in formatted["content"]


# -- Async Shield --

def test_async_shield_basic():
    shield = AsyncWarderBox(budget=5.0)

    @shield.async_tool(cost=0.1)
    async def async_query(sql: str):
        await asyncio.sleep(0.01)
        return f"result: {sql}"

    async def run():
        r = await async_query(sql="SELECT 1")
        assert not r.blocked
        assert "result: SELECT 1" in str(r.output)

        r2 = await async_query(sql="DROP TABLE users")
        assert r2.blocked

    asyncio.run(run())


def test_async_shield_sync_fallback():
    shield = AsyncWarderBox(budget=5.0)

    @shield.async_tool(cost=0.1)
    def sync_fn(x: int):
        return x * 2

    async def run():
        r = await sync_fn(x=21)
        assert not r.blocked
        assert r.output == 42

    asyncio.run(run())


# -- Config with rate limits and webhooks --

def test_from_config_with_rate_limits():
    events = []

    shield = WarderBox.from_config({
        "budget": 10.0,
        "global_rpm": 5,
        "tool_limits": {
            "send_email": {"rpm": 2, "rph": 10},
        },
    })
    shield.notifier.on("rate_limited", lambda e, d: events.append(d))

    @shield.tool(cost=0.01)
    def send_email(to: str):
        return "sent"

    send_email(to="a@b.com")
    send_email(to="c@d.com")
    r3 = send_email(to="e@f.com")
    assert r3.blocked
    assert len(events) == 1


if __name__ == "__main__":
    test_rate_limiter_global_rpm()
    test_rate_limiter_per_tool()
    test_event_notifier_callbacks()
    test_event_notifier_wildcard()
    test_shield_emits_events()
    test_anthropic_middleware()
    test_anthropic_format_tool_result()
    test_async_shield_basic()
    test_async_shield_sync_fallback()
    test_from_config_with_rate_limits()
    print("All Phase 3 tests passed!")
