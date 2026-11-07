"""Async version of WarderBox for use with asyncio-based agent frameworks."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from typing import Any

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.chaos import FaultType
from warderbox.guards import CallContext
from warderbox.retry import RetryPolicy
from warderbox.shield import WarderBox, _canonicalize_args
from warderbox.timeout import InFlight
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
        actual_cost: Callable[[Any], float] | None = None,
    ) -> Callable:

        def decorator(func: Callable) -> Callable:
            tool_name = func.__name__

            if actual_cost is not None:
                self._tool_actual_costs[tool_name] = actual_cost

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
        ctx = CallContext(
            shield=self, action=action, tool=action.tool_name, cost=cost,
            args=args, kwargs=kwargs, risk_override=risk_override,
        )
        # The guards are synchronous and may block (human-approval wait,
        # thread-based fallback); run them off the event loop so concurrent
        # async tasks are not frozen while one call waits for approval.
        # Shielded so a cancellation mid-way cannot orphan a reservation in
        # the worker thread.
        pre = asyncio.ensure_future(asyncio.to_thread(self._enter_guards, ctx))
        try:
            early = await asyncio.shield(pre)
        except asyncio.CancelledError:
            # The guards keep running; release whatever they reserved once
            # they finish, then propagate the cancellation.
            pre.add_done_callback(lambda fut: self._release_after_cancel(ctx, fut))
            raise

        if early is not None:
            return early

        return await self._async_run(ctx, func, retry_policy)

    def _release_after_cancel(self, ctx: CallContext, fut: Any) -> None:
        try:
            early = fut.result()
        except BaseException:
            return  # the pipeline already unwound
        if early is None:
            self._unwind(ctx)

    async def _async_run(
        self, ctx: CallContext, func: Callable, retry_policy: RetryPolicy | None
    ) -> ActionResult:
        self._begin_execution(ctx)
        try:
            try:
                fault = self._chaos_fault(ctx)
                if fault is not None:
                    await self._chaos.ainject(fault, ctx.tool)
                # Protected tools must not auto-retry (avoids duplicate side effects).
                policy = None if ctx.protected else retry_policy
                attempts = (policy.max_retries + 1) if policy else 1
                if policy and not policy.overlap_on_timeout:
                    ctx.inflight = InFlight()
                for attempt in range(attempts):
                    try:
                        ctx.started = True
                        output = await self._async_invoke(ctx, func)
                        break
                    except Exception as e:
                        if policy and policy.should_retry(e, attempt):
                            await asyncio.sleep(policy.delay_for(attempt))
                            continue
                        raise
                if fault is not None and fault.fault_type is FaultType.CORRUPT_OUTPUT:
                    output = self._chaos.corrupt(output)
                result = self._validated_result(ctx, output)
            except ToolTimeoutError as e:
                result = self._tool_failed(ctx, e, timed_out=True)
            except Exception as e:
                result = self._tool_failed(ctx, e)
            return self._finish_call(ctx, result)
        except BaseException as e:
            message = (
                "Cancelled" if isinstance(e, asyncio.CancelledError)
                else self._interrupted_message(ctx, e)
            )
            self._abort_call(ctx, e, message)
            raise

    async def _async_invoke(self, ctx: CallContext, func: Callable) -> Any:
        if self._timeout and self._timeout.policy.isolates(ctx.tool):
            # Runs in a worker process that is terminated at the deadline.
            return await asyncio.to_thread(
                self._timeout.execute, ctx.tool, func, ctx.args, ctx.kwargs
            )

        timeout_s = self._timeout.policy.get_timeout(ctx.tool) if self._timeout else None
        if inspect.iscoroutinefunction(func):
            # A coroutine is really cancelled at the deadline.
            coro = func(*ctx.args, **ctx.kwargs)
        else:
            # A thread cannot be cancelled: a retry resumes the same attempt.
            attempt = ctx.inflight.take() if ctx.inflight else None
            if attempt is None:
                loop = asyncio.get_running_loop()
                attempt = loop.run_in_executor(
                    None, lambda: func(*ctx.args, **ctx.kwargs)
                )
            if ctx.inflight is not None:
                attempt.add_done_callback(
                    lambda done: done.cancelled() or done.exception()
                )
                coro = asyncio.shield(attempt)
            else:
                coro = attempt

        if self._timeout:
            try:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            except TimeoutError:
                if not inspect.iscoroutinefunction(func) and ctx.inflight is not None:
                    ctx.inflight.keep(attempt)
                raise ToolTimeoutError(
                    f"Tool '{ctx.tool}' exceeded {timeout_s}s timeout"
                )
        return await coro
