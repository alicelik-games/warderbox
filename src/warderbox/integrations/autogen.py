"""WarderBox integration for Microsoft AutoGen.

Zero-dependency: does not import autogen. Provides a wrapper that
intercepts AutoGen function calls through WarderBox.

Usage:
    from warderbox import WarderBox
    from warderbox.integrations.autogen import shield_autogen_functions

    shield = WarderBox(budget=5.00)

    # Your functions that AutoGen agents will call
    def search(query: str) -> str:
        return f"results for {query}"

    def run_code(code: str) -> str:
        return exec_sandbox(code)

    # Wrap them with shield protection
    shielded = shield_autogen_functions(
        shield=shield,
        functions={"search": search, "run_code": run_code},
        cost_map={"search": 0.05, "run_code": 0.10},
    )

    # Use shielded["search"] and shielded["run_code"] in your AutoGen config
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from warderbox.action import Action, ActionResult
from warderbox.shield import WarderBox


def shield_autogen_functions(
    shield: WarderBox,
    functions: dict[str, Callable],
    cost_map: dict[str, float] | None = None,
) -> dict[str, Callable]:
    """Wrap AutoGen-compatible functions with WarderBox protection.

    Returns a dict of wrapped functions with the same signatures.
    When a function is blocked, it returns a string explaining why
    (AutoGen expects string returns from tool functions).
    """
    cost_map = cost_map or {}
    wrapped = {}

    for name, func in functions.items():
        cost = cost_map.get(name, 0.0)
        wrapped[name] = _wrap_function(shield, name, func, cost)

    return wrapped


def _wrap_function(
    shield: WarderBox,
    name: str,
    func: Callable,
    cost: float,
) -> Callable:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        action = Action(
            tool_name=name,
            args=kwargs if kwargs else {"args": list(args)},
            cost=cost,
        )

        result = shield._execute(
            action=action,
            func=func,
            args=args,
            kwargs=kwargs,
            cost=cost,
            risk_override=None,
        )

        if result.blocked:
            return f"[BLOCKED by WarderBox] {result.block_reason}"
        if result.error:
            return f"[ERROR] {result.error}"
        return str(result.output) if result.output is not None else ""

    return wrapper


class AutoGenShieldProxy:
    """A proxy that wraps an AutoGen agent's function map.

    Usage:
        proxy = AutoGenShieldProxy(shield, cost_map={"search": 0.05})

        # In your AutoGen ConversableAgent config:
        agent = ConversableAgent(
            name="assistant",
            function_map=proxy.wrap(original_function_map),
        )
    """

    def __init__(
        self,
        shield: WarderBox,
        cost_map: dict[str, float] | None = None,
    ) -> None:
        self._shield = shield
        self._cost_map = cost_map or {}

    def wrap(self, function_map: dict[str, Callable]) -> dict[str, Callable]:
        """Wrap all functions in an AutoGen function_map."""
        return shield_autogen_functions(
            self._shield, function_map, self._cost_map
        )
