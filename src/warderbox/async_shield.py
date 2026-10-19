"""Async version of WarderBox for use with asyncio-based agent frameworks."""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.shield import WarderBox


class AsyncWarderBox(WarderBox):
    """WarderBox with async tool support.

    Usage:
        shield = AsyncWarderBox(budget=5.00)

        @shield.async_tool(cost=0.03)
        async def query_db(sql: str) -> str:
            return await db.execute(sql)

        result = await query_db(sql="SELECT * FROM users")
    """

    def async_tool(
        self,
        fn: Callable | None = None,
        *,
        cost: float = 0.0,
        risk: RiskLevel | None = None,
        snapshot_state: Callable[[], Any] | None = None,
        restore_state: Callable[[Any], None] | None = None,
    ) -> Callable:

        def decorator(func: Callable) -> Callable:
            tool_name = func.__name__

            if snapshot_state and restore_state:
                self._tool_states[tool_name] = {
                    "capture": snapshot_state,
                    "restore": restore_state,
                }

            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> ActionResult:
                action = Action(
                    tool_name=tool_name,
                    args=kwargs if kwargs else {"args": list(args)},
                    cost=cost,
                )
                return await self._async_execute(action, func, args, kwargs, cost, risk)

            wrapper._shield_tool = True
            wrapper._tool_name = tool_name
            return wrapper

        if fn is not None:
            return decorator(fn)
        return decorator

    async def _async_execute(
        self,
        action: Action,
        func: Callable,
        args: tuple,
        kwargs: dict,
        cost: float,
        risk_override: RiskLevel | None,
    ) -> ActionResult:
        detected_risk, reasons = self._rules.evaluate(action)
        action.risk = risk_override or detected_risk

        if action.risk == RiskLevel.CRITICAL:
            result = ActionResult(
                action=action,
                blocked=True,
                block_reason=f"CRITICAL risk blocked: {'; '.join(reasons)}",
            )
            self._history.append(result)
            if self._logger:
                self._logger.log(result)
            self._notifier.emit("action_blocked", {
                "tool": action.tool_name, "reason": result.block_reason,
            })
            return result

        if action.risk == RiskLevel.HIGH and self._on_block:
            allowed = self._on_block(action, reasons)
            if not allowed:
                result = ActionResult(
                    action=action,
                    blocked=True,
                    block_reason=f"Blocked by user: {'; '.join(reasons)}",
                )
                self._history.append(result)
                if self._logger:
                    self._logger.log(result)
                self._notifier.emit("action_blocked", {
                    "tool": action.tool_name, "reason": result.block_reason,
                })
                return result

        if self._budget.would_exceed(cost):
            result = ActionResult(
                action=action,
                blocked=True,
                block_reason=f"Budget exceeded (spent: ${self._budget.spent:.2f}, "
                f"budget: ${self._budget.budget:.2f}, action cost: ${cost:.2f})",
            )
            self._history.append(result)
            if self._logger:
                self._logger.log(result)
            self._notifier.emit("budget_exceeded", {
                "tool": action.tool_name, "spent": self._budget.spent,
                "budget": self._budget.budget,
            })
            return result

        exceeded, rate_reason = self._rate_limiter.would_exceed(action.tool_name)
        if exceeded:
            result = ActionResult(
                action=action,
                blocked=True,
                block_reason=rate_reason,
            )
            self._history.append(result)
            if self._logger:
                self._logger.log(result)
            self._notifier.emit("rate_limited", {
                "tool": action.tool_name, "reason": rate_reason,
            })
            return result

        snapshot_id = None
        if self._auto_snapshot and action.tool_name in self._tool_states:
            state = self._tool_states[action.tool_name]["capture"]()
            snap = self._snapshots.capture(action.id, action.tool_name, state)
            snapshot_id = snap.id

        if self.is_test_mode:
            result = ActionResult(
                action=action,
                output=f"[TEST MODE] Would execute: {action.description}",
                snapshot_id=snapshot_id,
                cost=cost,
            )
            self._budget.record(action.id, action.tool_name, cost)
            self._rate_limiter.record(action.tool_name)
            self._history.append(result)
            if self._logger:
                self._logger.log(result)
            return result

        try:
            if inspect.iscoroutinefunction(func):
                output = await func(*args, **kwargs)
            else:
                loop = asyncio.get_event_loop()
                output = await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            result = ActionResult(
                action=action,
                output=output,
                snapshot_id=snapshot_id,
                cost=cost,
            )
        except Exception as e:
            result = ActionResult(
                action=action,
                error=str(e),
                snapshot_id=snapshot_id,
                cost=cost,
            )
            self._notifier.emit("action_error", {
                "tool": action.tool_name, "error": str(e),
            })

        self._budget.record(action.id, action.tool_name, cost)
        self._rate_limiter.record(action.tool_name)
        self._history.append(result)
        if self._logger:
            self._logger.log(result)

        if result.success:
            self._notifier.emit("action_completed", {
                "tool": action.tool_name, "cost": cost,
            })

        if self._budget.warning:
            self._notifier.emit("budget_warning", {
                "spent": self._budget.spent, "budget": self._budget.budget,
            })

        return result
