"""Async version of WarderBox for use with asyncio-based agent frameworks."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Callable
from typing import Any

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.retry import RetryPolicy
from warderbox.shield import WarderBox, _canonicalize_args
from warderbox.timeout import TimeoutError as ToolTimeoutError


class AsyncWarderBox(WarderBox):
    """WarderBox with async tool support.

    Shares the exact same guard pipeline as the synchronous shield
    (capabilities, idempotency, circuit breaker + fallback, risk rules,
    approval, budget reservation, rate/quota limits, snapshots, test mode,
    output validation, timeout, retry, tracing, estimator) — only the
    invocation step differs.

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
        retry: RetryPolicy | None = None,
        reversible: bool | None = None,
    ) -> Callable:

        def decorator(func: Callable) -> Callable:
            tool_name = func.__name__

            try:
                sig = inspect.signature(func)
            except (ValueError, TypeError):
                sig = None

            is_reversible = reversible
            if is_reversible is None and snapshot_state and restore_state:
                is_reversible = True

            if snapshot_state and restore_state:
                self._tool_states[tool_name] = {
                    "capture": snapshot_state,
                    "restore": restore_state,
                }

            self._tool_reversibility[tool_name] = is_reversible
            self._estimator.set_tool_cost(tool_name, cost)

            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> ActionResult:
                action = Action(
                    tool_name=tool_name,
                    args=_canonicalize_args(sig, args, kwargs),
                    cost=cost,
                )
                return await self._async_execute(
                    action, func, args, kwargs, cost, risk, retry
                )

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
        retry_policy: RetryPolicy | None = None,
    ) -> ActionResult:
        # _preflight is synchronous and may block (human-approval wait,
        # thread-based fallback); run it off the event loop so concurrent
        # async tasks are not frozen while one call waits for approval.
        # It is shielded so a cancellation mid-preflight cannot leave a
        # reservation orphaned in the worker thread.
        pre = asyncio.ensure_future(
            asyncio.to_thread(self._preflight, action, cost, risk_override, args, kwargs)
        )
        try:
            early, reservation, snapshot_id = await asyncio.shield(pre)
        except asyncio.CancelledError:
            # The preflight keeps running; release whatever it reserved once
            # it finishes, then propagate the cancellation.
            pre.add_done_callback(
                lambda fut: self._release_preflight(action, fut)
            )
            raise

        if early is not None:
            return early

        return await self._async_run(
            action, func, args, kwargs, cost, retry_policy,
            reservation, snapshot_id,
        )

    def _release_preflight(self, action: Action, fut: Any) -> None:
        try:
            _early, reservation, _snap = fut.result()
        except BaseException:
            return
        if reservation is None:
            return
        self._budget.release(reservation.reservation_id)
        self._rate_limiter.release(action.tool_name)
        if self._quota:
            self._quota.release(action.tool_name)
        if self._idempotency and self._idempotency.is_protected(action.tool_name):
            self._idempotency.fail(action.tool_name, action.args, keep=False)

    async def _async_run(
        self,
        action: Action,
        func: Callable,
        args: tuple,
        kwargs: dict,
        cost: float,
        retry_policy: RetryPolicy | None,
        reservation: Any,
        snapshot_id: str | None,
    ) -> ActionResult:
        span = self._start_span(
            f"tool.{action.tool_name}",
            {"tool.name": action.tool_name, "tool.cost": cost,
             "tool.risk": action.risk.value},
        )

        t0 = time.time()

        # Tracks whether the tool body actually started running, so a
        # cancellation can decide between releasing the idempotency marker
        # (nothing ran) and keeping it (a side effect may have landed).
        started = {"v": False}
        try:
            return await self._async_run_body(
                action, func, args, kwargs, cost, retry_policy,
                reservation, snapshot_id, span, t0, started,
            )
        except BaseException as e:
            cancelled = isinstance(e, asyncio.CancelledError)
            message = (
                "Cancelled" if cancelled
                else f"Interrupted: {type(e).__name__}: {e}"
            )
            self._end_span(span, "error", {"error.message": message})
            result = ActionResult(
                action=action, error=message, snapshot_id=snapshot_id,
            )
            self._append_history(result)
            self._log(result)
            self._budget.release(reservation.reservation_id)
            self._rate_limiter.release(action.tool_name)
            if self._quota:
                self._quota.release(action.tool_name)
            if self._idempotency and self._idempotency.is_protected(action.tool_name):
                try:
                    self._idempotency.fail(
                        action.tool_name, action.args, keep=started["v"]
                    )
                except Exception:
                    pass
            raise

    async def _async_run_body(
        self,
        action: Action,
        func: Callable,
        args: tuple,
        kwargs: dict,
        cost: float,
        retry_policy: RetryPolicy | None,
        reservation: Any,
        snapshot_id: str | None,
        span: Any,
        t0: float,
        started: dict,
    ) -> ActionResult:
        result: ActionResult | None = None

        if self._chaos and self._chaos.enabled:
            fault = self._chaos.should_inject(action.tool_name, action.args)
            if fault:
                try:
                    self._chaos.inject(fault, action.tool_name)
                except Exception as e:
                    if self._circuit_breaker:
                        self._circuit_breaker.record_failure(action.tool_name)
                    self._notifier.emit("action_error", {
                        "tool": action.tool_name, "error": str(e),
                    })
                    result = ActionResult(
                        action=action, error=str(e),
                        snapshot_id=snapshot_id, cost=cost,
                    )

        if result is None:
            # Protected tools must not auto-retry (avoids duplicate side effects).
            effective_retry = retry_policy
            if (
                retry_policy
                and self._idempotency
                and self._idempotency.is_protected(action.tool_name)
            ):
                effective_retry = None

            attempts = (effective_retry.max_retries + 1) if effective_retry else 1
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    started["v"] = True
                    output = await self._async_invoke(action, func, args, kwargs)
                    result = self._apply_output_validation(
                        action, output, snapshot_id, cost
                    )
                    break
                except Exception as e:
                    last_exc = e
                    if effective_retry and effective_retry.should_retry(e, attempt):
                        await asyncio.sleep(effective_retry.delay_for(attempt))
                        continue
                    break

            if result is None:
                if self._circuit_breaker:
                    self._circuit_breaker.record_failure(action.tool_name)
                if isinstance(last_exc, ToolTimeoutError):
                    self._notifier.emit("action_timeout", {
                        "tool": action.tool_name, "error": str(last_exc),
                    })
                else:
                    self._notifier.emit("action_error", {
                        "tool": action.tool_name, "error": str(last_exc),
                    })
                result = ActionResult(
                    action=action, error=str(last_exc),
                    snapshot_id=snapshot_id, cost=cost,
                )

        return self._finalize(action, result, reservation, snapshot_id, t0, span)

    async def _async_invoke(
        self, action: Action, func: Callable, args: tuple, kwargs: dict
    ) -> Any:
        if inspect.iscoroutinefunction(func):
            coro = func(*args, **kwargs)
        else:
            loop = asyncio.get_running_loop()
            coro = loop.run_in_executor(None, lambda: func(*args, **kwargs))

        if self._timeout:
            timeout_s = self._timeout.policy.get_timeout(action.tool_name)
            try:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            except TimeoutError:
                raise ToolTimeoutError(
                    f"Tool '{action.tool_name}' exceeded {timeout_s}s timeout"
                )
        return await coro
