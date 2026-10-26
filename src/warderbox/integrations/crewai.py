"""WarderBox integration for CrewAI.

Zero-dependency: does not import crewai. Provides a ShieldedCrewTool
that wraps any CrewAI-compatible tool through WarderBox.

Usage:
    from warderbox import WarderBox
    from warderbox.integrations.crewai import ShieldedCrewTool, shield_crewai_tools

    shield = WarderBox(budget=5.00)

    # Wrap individual tools
    tool = ShieldedCrewTool(
        shield=shield,
        name="search",
        description="Search the web",
        func=my_search_fn,
        cost=0.05,
    )

    # Or wrap a list of existing CrewAI tools
    shielded = shield_crewai_tools(shield, existing_tools, cost_map={"search": 0.05})
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from warderbox.action import Action
from warderbox.shield import WarderBox, _canonicalize_args


def _signature(func: Callable) -> inspect.Signature | None:
    try:
        return inspect.signature(func)
    except (ValueError, TypeError):
        return None


class ShieldedCrewTool:
    """A CrewAI-compatible tool that routes execution through WarderBox.

    Mimics CrewAI's BaseTool interface without importing crewai.
    """

    def __init__(
        self,
        shield: WarderBox,
        name: str,
        description: str,
        func: Callable[..., str],
        cost: float = 0.0,
    ) -> None:
        self.shield = shield
        self.name = name
        self.description = description
        self._func = func
        self._cost = cost

    def run(self, *args: Any, **kwargs: Any) -> str:
        """Execute the tool through the shield. Returns string output.

        Positional args are passed through positionally (so a tool declared
        as ``def search(query): ...`` works); the action's args dict is
        canonicalized to the real parameter names for the rule engine.
        """
        action = Action(
            tool_name=self.name,
            args=_canonicalize_args(_signature(self._func), args, kwargs),
            cost=self._cost,
        )

        result = self.shield._execute(
            action=action,
            func=self._func,
            args=args,
            kwargs=kwargs,
            cost=self._cost,
            risk_override=None,
        )

        if result.blocked:
            return f"[BLOCKED] {result.block_reason}"
        if result.error:
            return f"[ERROR] {result.error}"
        return str(result.output) if result.output is not None else ""

    def _run(self, *args: Any, **kwargs: Any) -> str:
        return self.run(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return self.run(*args, **kwargs)


def shield_crewai_tools(
    shield: WarderBox,
    tools: list[Any],
    cost_map: dict[str, float] | None = None,
) -> list[ShieldedCrewTool]:
    """Wrap a list of existing CrewAI tools with WarderBox protection.

    Each tool must have .name, .description, and ._run or .run attributes.
    """
    cost_map = cost_map or {}
    shielded = []

    for tool in tools:
        name = getattr(tool, "name", str(tool))
        description = getattr(tool, "description", "")
        func = getattr(tool, "_run", None) or getattr(tool, "run", None)

        if func is None:
            raise ValueError(f"Tool '{name}' has no _run or run method")

        shielded.append(ShieldedCrewTool(
            shield=shield,
            name=name,
            description=description,
            func=func,
            cost=cost_map.get(name, 0.0),
        ))

    return shielded
