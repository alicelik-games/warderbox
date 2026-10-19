"""WarderBox integration for Anthropic Claude SDK (tool_use).

Zero-dependency: does not import anthropic. Works with the tool_use
response format from Claude's Messages API.

Usage:
    from warderbox import WarderBox
    from warderbox.integrations.anthropic_sdk import AnthropicShieldMiddleware

    shield = WarderBox(budget=5.00)
    middleware = AnthropicShieldMiddleware(
        shield=shield,
        executors={"query_db": query_db_fn},
        cost_map={"query_db": 0.03},
    )

    # In your agent loop, when Claude returns a tool_use block:
    for block in response.content:
        if block.type == "tool_use":
            result = middleware.handle_tool_use(block)
"""

from __future__ import annotations

import json
from typing import Any, Callable

from warderbox.action import Action, ActionResult
from warderbox.shield import WarderBox


class AnthropicShieldMiddleware:
    """Intercepts Anthropic Claude tool_use blocks through WarderBox."""

    def __init__(
        self,
        shield: WarderBox,
        executors: dict[str, Callable[..., Any]] | None = None,
        cost_map: dict[str, float] | None = None,
    ) -> None:
        self._shield = shield
        self._executors = executors or {}
        self._cost_map = cost_map or {}

    def register_tool(
        self, name: str, executor: Callable[..., Any], cost: float = 0.0
    ) -> None:
        """Register a tool executor and its cost."""
        self._executors[name] = executor
        if cost > 0:
            self._cost_map[name] = cost

    def handle_tool_use(self, tool_use_block: Any) -> ActionResult:
        """Process a Claude tool_use content block through the shield.

        Args:
            tool_use_block: A tool_use block from Claude's response.
                Expected attributes: name (str), input (dict), id (str).
                Also accepts a plain dict with those keys.
        """
        if isinstance(tool_use_block, dict):
            tool_name = tool_use_block["name"]
            tool_input = tool_use_block.get("input", {})
            tool_id = tool_use_block.get("id", "")
        else:
            tool_name = tool_use_block.name
            tool_input = tool_use_block.input
            tool_id = getattr(tool_use_block, "id", "")

        if isinstance(tool_input, str):
            tool_input = json.loads(tool_input)

        cost = self._cost_map.get(tool_name, 0.0)

        action = Action(
            tool_name=tool_name,
            args=tool_input,
            cost=cost,
        )

        executor = self._executors.get(tool_name)
        if executor is None:
            return ActionResult(
                action=action,
                blocked=True,
                block_reason=f"No executor registered for tool '{tool_name}'",
            )

        return self._shield._execute(
            action=action,
            func=executor,
            args=(),
            kwargs=tool_input,
            cost=cost,
            risk_override=None,
        )

    def format_tool_result(self, result: ActionResult) -> dict:
        """Format an ActionResult as a tool_result content block for Claude.

        Returns a dict suitable for the Messages API:
            {"type": "tool_result", "tool_use_id": ..., "content": ...}
        """
        if result.blocked:
            content = f"[BLOCKED] {result.block_reason}"
            is_error = True
        elif result.error:
            content = f"[ERROR] {result.error}"
            is_error = True
        else:
            content = str(result.output) if result.output is not None else ""
            is_error = False

        return {
            "type": "tool_result",
            "tool_use_id": result.action.id,
            "content": content,
            "is_error": is_error,
        }
