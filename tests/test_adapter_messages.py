"""What each framework adapter lets the model see."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from warderbox import OutputRule, OutputValidator, WarderBox
from warderbox.integrations.anthropic_sdk import AnthropicShieldMiddleware
from warderbox.integrations.autogen import _wrap_function
from warderbox.integrations.crewai import ShieldedCrewTool
from warderbox.integrations.langchain import ShieldedTool
from warderbox.integrations.openai_agents import OpenAIShieldMiddleware


def _shield_with_validator():
    shield = WarderBox(budget=5.0)
    validator = OutputValidator()
    validator.add_rule(OutputRule(
        name="contains_salary_data", validator=lambda out: False
    ))
    shield.set_output_validator(validator)
    return shield


def _anthropic(shield, func):
    middleware = AnthropicShieldMiddleware(shield, executors={"tool": func})
    block = SimpleNamespace(id="toolu_1", name="tool", input={"q": "x"})
    return middleware.format_tool_result(middleware.handle_tool_use(block))["content"]


def _openai(shield, func):
    middleware = OpenAIShieldMiddleware(shield, executors={"tool": func})
    call = SimpleNamespace(
        function=SimpleNamespace(name="tool", arguments=json.dumps({"q": "x"}))
    )
    return middleware.format_output(middleware.handle_tool_call(call))


ADAPTERS = {
    "langchain": lambda s, f: ShieldedTool(shield=s, name="tool", func=f).run("x"),
    "crewai": lambda s, f: ShieldedCrewTool(
        shield=s, name="tool", description="d", func=f
    ).run(q="x"),
    "autogen": lambda s, f: _wrap_function(s, "tool", f, 0.0)(q="x"),
    "anthropic": _anthropic,
    "openai": _openai,
}


@pytest.mark.parametrize("name", ADAPTERS)
def test_output_violation_details_do_not_reach_the_model(name):
    shield = _shield_with_validator()
    message = ADAPTERS[name](shield, lambda q: "salary: 100")
    assert message.startswith("[BLOCKED]")
    assert "salary" not in message.lower()
    assert "contains_salary_data" not in message
    # The operator still sees why.
    assert "contains_salary_data" in shield.history()[-1]["error"]


@pytest.mark.parametrize("name", ADAPTERS)
def test_block_reason_is_not_revealed_to_the_model(name):
    shield = WarderBox(budget=5.0)
    shield._rules.add_rule(_always_critical())
    ran = []
    message = ADAPTERS[name](shield, lambda q: ran.append(1))
    assert message == "[BLOCKED] Action blocked by WarderBox safety policy"
    assert "rule detail" not in message
    assert "ali@example.com" not in message
    assert not ran
    # The operator still gets the detailed rule reason.
    assert "rule detail" in shield.history(raw=True)[-1]["block_reason"]


def _always_critical():
    from warderbox.action import RiskLevel
    from warderbox.rules import Rule

    return Rule(
        name="always", check=lambda action: True,
        risk=RiskLevel.CRITICAL, reason="rule detail for ali@example.com",
    )


@pytest.mark.parametrize("name", ADAPTERS)
def test_tool_errors_reach_the_model_with_secrets_masked(name):
    def fails(q):
        raise RuntimeError("bad key sk-abcdefghijklmnopqrstuvwxyz for user@example.com")

    message = ADAPTERS[name](WarderBox(budget=5.0), fails)
    assert message.startswith("[ERROR]")
    assert "bad key" in message
    assert "abcdefghijklmnop" not in message
    assert "user@example.com" not in message


@pytest.mark.parametrize("name", ADAPTERS)
def test_success_output_is_passed_through(name):
    assert ADAPTERS[name](WarderBox(budget=5.0), lambda q: "fine") == "fine"
