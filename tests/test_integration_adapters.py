"""P1-8: framework adapters must work with real tool signatures."""

from __future__ import annotations

from warderbox import WarderBox
from warderbox.integrations.anthropic_sdk import AnthropicShieldMiddleware
from warderbox.integrations.crewai import ShieldedCrewTool
from warderbox.integrations.langchain import ShieldedTool


def test_anthropic_tool_result_id_matches_tool_use():
    shield = WarderBox(budget=5.0)
    mw = AnthropicShieldMiddleware(shield)
    mw.register_tool("lookup", lambda q: f"result:{q}", cost=0.01)

    block = {"name": "lookup", "input": {"q": "hi"}, "id": "toolu_ABC123"}
    result = mw.handle_tool_use(block)
    formatted = mw.format_tool_result(result)
    assert formatted["tool_use_id"] == "toolu_ABC123"


def test_langchain_string_input_positional():
    shield = WarderBox(budget=5.0)

    def run_sql(sql: str) -> str:
        return f"ran:{sql}"

    tool = ShieldedTool(shield=shield, name="run_sql", func=run_sql, cost=0.01)
    out = tool.run("SELECT 1")
    assert out == "ran:SELECT 1"


def test_langchain_blocks_destructive_sql_positional():
    shield = WarderBox(budget=5.0)

    def run_sql(sql: str) -> str:
        return "ran"

    tool = ShieldedTool(shield=shield, name="run_sql", func=run_sql, cost=0.01)
    assert tool.run("DROP TABLE users").startswith("[BLOCKED]")


def test_crewai_positional_query():
    shield = WarderBox(budget=5.0)

    def search(query: str) -> str:
        return f"found:{query}"

    tool = ShieldedCrewTool(shield=shield, name="search", description="", func=search, cost=0.01)
    assert tool.run("cats") == "found:cats"
