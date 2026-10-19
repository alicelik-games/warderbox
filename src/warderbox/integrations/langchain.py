"""LangChain integration.

Wraps LangChain tools so every invocation passes through WarderBox.

Usage:
    from warderbox import WarderBox
    from warderbox.integrations.langchain import ShieldedTool

    shield = WarderBox(budget=5.00)

    # Wrap any LangChain-style tool
    safe_tool = ShieldedTool(
        shield=shield,
        name="run_sql",
        func=my_sql_function,
        description="Execute a SQL query",
        cost=0.05,
    )

    # Use in a LangChain agent
    agent = initialize_agent(tools=[safe_tool], ...)
"""

from __future__ import annotations

from typing import Any, Callable

from warderbox.action import Action, ActionResult
from warderbox.shield import WarderBox


class ShieldedTool:
    """A tool wrapper compatible with LangChain's tool interface.

    This class mimics LangChain's BaseTool without importing langchain,
    so warderbox has zero mandatory dependencies.

    When LangChain calls .run() or ._run(), the call goes through
    WarderBox's interceptor.
    """

    def __init__(
        self,
        shield: WarderBox,
        name: str,
        func: Callable,
        description: str = "",
        cost: float = 0.0,
        snapshot_state: Callable[[], Any] | None = None,
        restore_state: Callable[[Any], None] | None = None,
    ) -> None:
        self.shield = shield
        self.name = name
        self.func = func
        self.description = description
        self.cost = cost

        if snapshot_state and restore_state:
            self.shield._tool_states[name] = {
                "capture": snapshot_state,
                "restore": restore_state,
            }

    def run(self, tool_input: str | dict, **kwargs: Any) -> str:
        """LangChain calls this method."""
        if isinstance(tool_input, str):
            args = {"input": tool_input}
        else:
            args = tool_input

        action = Action(tool_name=self.name, args=args, cost=self.cost)
        result = self.shield._execute(action, self._call, (), args, self.cost, None)

        if result.blocked:
            return f"[BLOCKED] {result.block_reason}"
        if result.error:
            return f"[ERROR] {result.error}"
        return str(result.output)

    def _call(self, **kwargs: Any) -> Any:
        return self.func(**kwargs)

    # LangChain compatibility
    _run = run

    def __repr__(self) -> str:
        return f"ShieldedTool(name={self.name!r}, cost={self.cost})"


def shield_langchain_tools(
    shield: WarderBox,
    tools: list[Any],
    cost_map: dict[str, float] | None = None,
) -> list[ShieldedTool]:
    """Wrap a list of existing LangChain tools with WarderBox.

    Args:
        shield: The WarderBox instance.
        tools: List of LangChain BaseTool instances.
        cost_map: Optional {tool_name: cost_per_call} mapping.

    Returns:
        List of ShieldedTool wrappers.
    """
    if cost_map is None:
        cost_map = {}

    wrapped = []
    for tool in tools:
        name = getattr(tool, "name", str(tool))
        description = getattr(tool, "description", "")
        func = getattr(tool, "_run", getattr(tool, "run", None))

        if func is None:
            continue

        wrapped.append(
            ShieldedTool(
                shield=shield,
                name=name,
                func=func,
                description=description,
                cost=cost_map.get(name, 0.0),
            )
        )

    return wrapped
