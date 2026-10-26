"""OpenAI Agents SDK integration.

Wraps OpenAI function tools so every call passes through WarderBox.

Usage:
    from warderbox import WarderBox
    from warderbox.integrations.openai_agents import shield_openai_tools

    shield = WarderBox(budget=5.00)
    tools = [my_tool_a, my_tool_b]
    safe_tools = shield_openai_tools(shield, tools)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from warderbox.action import Action, ActionResult
from warderbox.shield import WarderBox


def shield_openai_tools(
    shield: WarderBox,
    tools: list[dict[str, Any]],
    cost_map: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Wrap a list of OpenAI-style function tool definitions.

    Each tool dict is expected to have:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

    This does NOT modify the schema — it returns a registry of shielded callables
    alongside the original tool definitions, so the caller can route execution
    through the shield.

    Args:
        shield: The WarderBox instance.
        tools: List of OpenAI tool definitions.
        cost_map: Optional {tool_name: cost_per_call} mapping.

    Returns:
        The same tool list (schemas unchanged for the API), plus a side effect:
        shield._openai_registry is populated with wrapped callables.
    """
    if cost_map is None:
        cost_map = {}

    if not hasattr(shield, "_openai_registry"):
        shield._openai_registry = {}

    for tool_def in tools:
        if tool_def.get("type") != "function":
            continue
        func_def = tool_def.get("function", {})
        name = func_def.get("name", "unknown")
        shield._openai_registry[name] = {
            "cost": cost_map.get(name, 0.0),
            "schema": func_def,
        }

    return tools


def execute_tool_call(
    shield: WarderBox,
    tool_name: str,
    arguments: str | dict,
    executor: Callable[..., Any],
) -> ActionResult:
    """Execute a tool call through the shield.

    This is the function you call when the model returns a tool_call.

    Args:
        shield: The WarderBox instance.
        tool_name: The function name from the tool call.
        arguments: The arguments (JSON string or dict).
        executor: The actual function to call.

    Returns:
        ActionResult with output, cost, and block status.
    """
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            args = {"raw": arguments}
    else:
        args = arguments

    registry = getattr(shield, "_openai_registry", {})
    cost = registry.get(tool_name, {}).get("cost", 0.0)

    action = Action(tool_name=tool_name, args=args, cost=cost)
    return shield._execute(action, executor, (), args, cost, None)


class OpenAIShieldMiddleware:
    """Middleware that sits between the OpenAI agent loop and tool execution.

    Usage:
        middleware = OpenAIShieldMiddleware(shield, tool_executors)

        # In your agent loop, when a tool call comes back:
        result = middleware.handle_tool_call(tool_call)
    """

    def __init__(
        self,
        shield: WarderBox,
        executors: dict[str, Callable],
        cost_map: dict[str, float] | None = None,
    ) -> None:
        self.shield = shield
        self.executors = executors
        self.cost_map = cost_map or {}

    def handle_tool_call(self, tool_call: Any) -> ActionResult:
        """Handle a single tool call from the OpenAI API response.

        Args:
            tool_call: An object with .function.name and .function.arguments
        """
        name = tool_call.function.name
        arguments = tool_call.function.arguments

        executor = self.executors.get(name)
        if executor is None:
            action = Action(tool_name=name, args={"raw": arguments})
            return ActionResult(
                action=action,
                blocked=True,
                block_reason=f"Unknown tool: {name}",
            )

        return execute_tool_call(self.shield, name, arguments, executor)
