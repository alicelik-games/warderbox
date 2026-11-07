"""WarderBox — the main class that ties together rollback, cost control, and testing."""

from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import time
import warnings
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Any

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.approval import ApprovalManager
from warderbox.budget import BudgetTracker
from warderbox.capabilities import CapabilityManager
from warderbox.chaos import ChaosEngine, FaultType
from warderbox.circuitbreaker import CircuitBreaker
from warderbox.config import parse_config
from warderbox.estimator import CostEstimator
from warderbox.fallback import FallbackManager
from warderbox.guards import Block, CallContext, Guard, default_guards
from warderbox.idempotency import IdempotencyGuard
from warderbox.logger import ActionLogger
from warderbox.masking import DataMasker
from warderbox.output_validation import OutputValidator
from warderbox.quota import QuotaManager
from warderbox.ratelimit import RateLimiter, ToolLimit
from warderbox.retry import RetryPolicy, execute_with_retry
from warderbox.rules import RuleEngine
from warderbox.snapshot import SnapshotStore
from warderbox.state import StateBackend, StateBackendError, backend_from_config
from warderbox.timeout import InFlight, TimeoutExecutor
from warderbox.timeout import TimeoutError as ToolTimeoutError
from warderbox.tracing import Tracer
from warderbox.webhooks import EventNotifier


class Mode(Enum):
    PRODUCTION = "production"
    TEST = "test"


OnBlockCallback = Callable[[Action, list[str]], bool]

def _resolve_maybe_async(value: Any, timeout: float | None = None) -> Any:
    """Run an awaitable to completion when no event loop is running.

    Fallback functions may be async; when a fallback is executed from a
    worker thread (the async pipeline runs the guards via asyncio.to_thread)
    there is no running loop, so we can drive the coroutine here. An optional
    timeout bounds the coroutine so an async fallback cannot hang forever.
    """
    if inspect.isawaitable(value):
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if timeout is not None:
                async def _run():
                    return await asyncio.wait_for(value, timeout=timeout)
                try:
                    return asyncio.run(_run())
                except TimeoutError as e:
                    from warderbox.timeout import TimeoutError as ToolTimeoutError
                    raise ToolTimeoutError(
                        f"Async fallback exceeded {timeout}s timeout"
                    ) from e
            return asyncio.run(value)
    return value


def _canonicalize_args(
    sig: inspect.Signature | None, args: tuple, kwargs: dict
) -> dict[str, Any]:
    """Map a positional+keyword call onto a ``{param_name: value}`` dict.

    Binding to the real parameter names means content rules, capability
    constraints, idempotency hashing and audit logs all see the same
    structure whether the tool was called positionally or by keyword.
    Falls back to a best-effort representation when the signature cannot
    bind the given arguments (e.g. builtins without a signature).
    """
    if sig is not None:
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            pass
    merged = dict(kwargs)
    if args:
        merged["args"] = list(args)
    return merged


def _config_audit_key(config: dict[str, Any]) -> str | None:
    """The key named by a policy. A named variable that is not set is an error,
    not a silent fall back to an unkeyed log."""
    if config.get("audit_key") is not None:
        return config["audit_key"]
    name = config.get("audit_key_env")
    if not name:
        return None
    value = os.environ.get(name)
    if not value:
        raise ValueError(
            f"audit_key_env: environment variable {name!r} is not set or empty"
        )
    return value


def _config_flag(config: dict[str, Any], name: str) -> bool:
    value = config.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value


class WarderBox:
    """Wrap agent tools with rollback, cost control, and testing.

    Usage:
        shield = WarderBox(budget=5.00, auto_snapshot=True)

        @shield.tool(cost=0.03)
        def query_db(sql: str) -> str:
            ...

        # Undo the last action
        shield.rollback()

        # See what happened
        shield.history()
    """

    def __init__(
        self,
        budget: float | None = None,
        auto_snapshot: bool = True,
        mode: str = "production",
        on_block: OnBlockCallback | None = None,
        max_snapshots: int = 1000,
        db_path: str | None = None,
        global_rpm: int | None = None,
        global_rph: int | None = None,
        tool_limits: dict[str, ToolLimit] | None = None,
        agent_name: str = "",
        max_history: int = 10000,
        allow_private_webhooks: bool = False,
        state_backend: StateBackend | None = None,
        namespace: str = "default",
        audit_key: str | bytes | None = None,
        require_audit_key: bool = False,
    ) -> None:
        # Keyless audit logs remain the backwards-compatible default. Opt in
        # to the stronger mode explicitly: require_audit_key=True raises if no
        # HMAC key is supplied. This avoids breaking existing
        # WarderBox(db_path=...) deployments on upgrade.
        if require_audit_key and not (audit_key or os.environ.get("WARDERBOX_AUDIT_KEY")):
            raise ValueError(
                "require_audit_key=True: pass audit_key= or set "
                "WARDERBOX_AUDIT_KEY (or opt out with require_audit_key=False)."
            )
        self._mode = Mode(mode)
        self._agent_name = agent_name
        self._auto_snapshot = auto_snapshot
        self._on_block = on_block
        self._rules = RuleEngine()
        self._state_backend = state_backend
        self._namespace = namespace
        self._budget = BudgetTracker(
            budget=budget, backend=state_backend, namespace=namespace,
        )
        self._budget.on_backend_error = self._backend_error
        self._guards: list[Guard] = default_guards()
        self._audit_log_errors = 0
        self._snapshots = SnapshotStore(max_snapshots=max_snapshots)
        self._max_history = max_history
        self._history: deque[ActionResult] = deque(maxlen=max_history)
        self._history_lock = threading.Lock()
        self._tool_states: dict[str, Any] = {}
        self._tool_reversibility: dict[str, bool | None] = {}
        self._tool_actual_costs: dict[str, Callable[[Any], float]] = {}
        # Audit logs and webhooks are masked by default. Call
        # set_masker(DataMasker(enabled=False)) to opt out explicitly.
        self._masker = DataMasker()
        self._logger = (
            ActionLogger(db_path, masker=self._masker, audit_key=audit_key)
            if db_path else None
        )
        self._rate_limiter = RateLimiter(
            global_rpm=global_rpm,
            global_rph=global_rph,
            tool_limits=tool_limits or {},
            backend=state_backend,
            namespace=namespace,
        )
        self._notifier = EventNotifier(
            masker=self._masker,
            allow_private_hosts=allow_private_webhooks,
        )
        self._circuit_breaker: CircuitBreaker | None = None
        self._capabilities: CapabilityManager | None = None
        self._approval: ApprovalManager | None = None
        self._tracer: Tracer | None = None
        self._chaos: ChaosEngine | None = None
        self._timeout: TimeoutExecutor | None = None
        self._idempotency: IdempotencyGuard | None = None
        self._output_validator: OutputValidator | None = None
        self._fallback: FallbackManager | None = None
        self._quota: QuotaManager | None = None
        self._anomaly: Any = None
        self._estimator = CostEstimator()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> WarderBox:
        """Create an WarderBox from a config dict."""
        parsed = parse_config(config)

        tool_limits = {}
        for name, lim in config.get("tool_limits", {}).items():
            tool_limits[name] = ToolLimit(
                rpm=lim.get("rpm"),
                rph=lim.get("rph"),
            )

        instance = cls(
            budget=parsed["budget"],
            auto_snapshot=parsed["auto_snapshot"],
            mode=parsed["mode"],
            max_snapshots=parsed["max_snapshots"],
            db_path=parsed.get("db_path"),
            global_rpm=config.get("global_rpm"),
            global_rph=config.get("global_rph"),
            tool_limits=tool_limits or None,
            state_backend=(
                backend_from_config(config["state"]) if config.get("state") else None
            ),
            namespace=config.get("namespace", "default"),
            audit_key=_config_audit_key(config),
            require_audit_key=_config_flag(config, "require_audit_key"),
        )
        for rule in parsed["extra_rules"]:
            instance._rules.add_rule(rule)

        for wh in config.get("webhooks", []):
            instance._notifier.add_webhook(
                url=wh["url"],
                events=set(wh.get("events", [])) or None,
                headers=wh.get("headers", {}),
            )

        return instance

    # -- Properties --

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def is_test_mode(self) -> bool:
        return self._mode == Mode.TEST

    @property
    def budget_summary(self) -> dict:
        return self._budget.summary()

    @property
    def notifier(self) -> EventNotifier:
        return self._notifier

    def set_masker(self, masker: DataMasker) -> None:
        """Attach a DataMasker for log/webhook PII redaction."""
        self._masker = masker
        if self._logger:
            self._logger._masker = masker
        self._notifier.set_masker(masker)

    def set_circuit_breaker(self, cb: CircuitBreaker) -> None:
        self._circuit_breaker = cb

    def set_capabilities(self, caps: CapabilityManager) -> None:
        self._capabilities = caps

    def set_approval(self, approval: ApprovalManager) -> None:
        self._approval = approval

    def set_tracer(self, tracer: Tracer) -> None:
        self._tracer = tracer

    def set_chaos(self, chaos: ChaosEngine) -> None:
        self._chaos = chaos
        if not self.is_test_mode and chaos.enabled and chaos.allow_production:
            warnings.warn(
                "ChaosEngine attached in production mode: its faults will hit "
                "real tool calls. Use ChaosEngine(allow_production=False) to "
                "keep it inert outside test mode.",
                RuntimeWarning,
                stacklevel=2,
            )

    def set_timeout(self, timeout: TimeoutExecutor) -> None:
        self._timeout = timeout

    def set_idempotency(self, guard: IdempotencyGuard) -> None:
        self._idempotency = guard

    def set_output_validator(self, validator: OutputValidator) -> None:
        self._output_validator = validator

    def set_fallback(self, fallback: FallbackManager) -> None:
        self._fallback = fallback

    def set_quota(self, quota: QuotaManager) -> None:
        # A shield with shared state shares its quotas too, unless the manager
        # has its own storage.
        if (
            self._state_backend is not None
            and quota._backend is None
            and not quota._persist_path
        ):
            quota.use_backend(self._state_backend, self._namespace)
        self._quota = quota

    def set_actual_cost(self, tool_name: str, fn: Callable[[Any], float]) -> None:
        """Report a tool's real charge from its output. For tools registered by
        a framework adapter, where the ``actual_cost=`` decorator argument is
        not available. See :func:`warderbox.budget.cost_from_usage`."""
        self._tool_actual_costs[tool_name] = fn

    def set_anomaly_detector(self, detector: Any) -> None:
        """Attach an AnomalyDetector; cost of each executed action is recorded."""
        self._anomaly = detector

    @property
    def estimator(self) -> CostEstimator:
        return self._estimator

    def estimate(self, plan: list[str]) -> dict:
        """Estimate cost and duration before execution.

        Self-improving: gets smarter the more your agents run.
        """
        return self._estimator.estimate(plan).summary()

    # -- Tool decorator --

    def tool(
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

            if inspect.iscoroutinefunction(func):
                warnings.warn(
                    f"@shield.tool wraps '{tool_name}' which is an async "
                    f"function; the sync pipeline will not await it. Use "
                    f"AsyncWarderBox.async_tool for coroutines.",
                    RuntimeWarning,
                    stacklevel=2,
                )

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
            def wrapper(*args: Any, **kwargs: Any) -> ActionResult:
                action = Action(
                    tool_name=tool_name,
                    args=_canonicalize_args(sig, args, kwargs),
                    cost=cost,
                )

                return self._execute(action, func, args, kwargs, cost, risk, retry)

            wrapper._shield_tool = True
            wrapper._tool_name = tool_name
            return wrapper

        if fn is not None:
            return decorator(fn)
        return decorator

    # -- Core execution --

    def _append_history(self, result: ActionResult) -> None:
        with self._history_lock:
            self._history.append(result)

    def _start_span(self, name: str, attributes: dict) -> Any:
        """Start a tracer span, isolating a faulty tracer from the call."""
        if not self._tracer:
            return None
        try:
            return self._tracer.start_span(name, attributes=attributes)
        except Exception:
            return None

    def _end_span(self, span: Any, status: str, attributes: dict | None = None) -> None:
        if span is None or not self._tracer:
            return
        try:
            for k, v in (attributes or {}).items():
                span.set_attribute(k, v)
            self._tracer.end_span(span, status)
        except Exception:
            pass

    def _log(self, result: ActionResult) -> None:
        """Write to the audit log. A logging failure must not break the tool
        call, but it must not pass silently either: an audit gap is reported
        through the audit_log_error event and stats()."""
        if not self._logger:
            return
        try:
            self._logger.log(result)
        except Exception as e:
            with self._history_lock:
                self._audit_log_errors += 1
            self._notifier.emit("audit_log_error", {
                "tool": result.action.tool_name,
                "action_id": result.action.id,
                "error": str(e),
            })

    def _backend_error(self, error: Exception) -> None:
        self._notifier.emit("state_backend_error", {"error": str(error)})

    def _consult_on_block(self, action: Action, reasons: list[str]) -> bool:
        """Call the on_block callback defensively. A raising callback is
        treated as a denial (fail closed) and never crashes the call."""
        try:
            return bool(self._on_block(action, reasons))
        except Exception:
            return False

    def _blocked(
        self, ctx: CallContext, reason: str, event: str | None
    ) -> ActionResult:
        result = self._make_result(ctx, blocked=True, block_reason=reason)
        self._append_history(result)
        self._log(result)
        if event is not None:
            payload = {"tool": ctx.action.tool_name, "reason": reason}
            if ctx.is_fallback:
                payload["fallback"] = ctx.tool
            self._notifier.emit(event, payload)
        return result

    # -- Guard pipeline --

    def add_guard(self, guard: Guard, before: str = "budget") -> None:
        """Add a custom guard ahead of a built-in one (by default, before any
        budget is reserved). See :mod:`warderbox.guards`."""
        names = [g.name for g in self._guards]
        if before not in names:
            raise ValueError(f"Unknown guard '{before}'. Available: {names}")
        self._guards.insert(names.index(before), guard)

    def _enter_guards(self, ctx: CallContext) -> ActionResult | None:
        """Run the guards. None means the tool should run now, with the
        reservations recorded in ``ctx``. Anything else is the final result."""
        guard: Guard | None = None
        try:
            for guard in self._guards:
                if ctx.is_fallback and not guard.applies_to_fallback:
                    continue
                outcome = guard.check(ctx)
                if outcome is None:
                    outcome = guard.reserve(ctx)
                if outcome is None:
                    ctx.entered.append(guard)
                    continue
                self._unwind(ctx)
                if isinstance(outcome, Block):
                    return self._blocked(ctx, outcome.reason, outcome.event)
                return outcome
        except Exception as e:
            self._unwind(ctx)
            message = f"{guard.failure_label}: {e}"
            if ctx.is_fallback:
                ctx.charge = False
                return self._fallback_guard_failed(ctx, message)
            # Fail closed: the tool does not run, and the agent gets a result.
            self._notifier.emit("guard_error", {
                "tool": ctx.tool, "guard": guard.name, "error": message,
            })
            return self._blocked(ctx, message, "action_blocked")
        except BaseException as e:
            self._unwind(ctx)
            if ctx.is_fallback:
                self._fallback_guard_failed(ctx, f"Fallback interrupted: {e}")
            raise
        return None

    def _unwind(self, ctx: CallContext) -> None:
        """Release everything held, newest first. One failing guard must not
        stop the others from letting go."""
        while ctx.entered:
            guard = ctx.entered.pop()
            try:
                guard.release(ctx)
            except Exception as e:
                self._notifier.emit("guard_error", {
                    "tool": ctx.tool, "guard": guard.name, "error": str(e),
                })

    def _make_result(self, ctx: CallContext, **fields: Any) -> ActionResult:
        fields.setdefault("snapshot_id", ctx.snapshot_id)
        if ctx.is_fallback:
            fields.setdefault("fallback_tool", ctx.tool)
            fields.setdefault("reversible", self._tool_reversibility.get(ctx.tool))
        return ActionResult(action=ctx.action, **fields)

    def _fallback_guard_failed(self, ctx: CallContext, message: str) -> ActionResult:
        result = self._make_result(ctx, error=message)
        self._append_history(result)
        self._log(result)
        self._notifier.emit("action_fallback_failed", {
            "tool": ctx.action.tool_name, "fallback": ctx.tool, "error": message,
        })
        return result

    def _deduplicated(self, ctx: CallContext, cached: Any) -> ActionResult:
        result = ActionResult(
            action=ctx.action, blocked=True,
            block_reason="Duplicate call blocked (cached result available)",
            output=cached,
            fallback_tool=ctx.tool if ctx.is_fallback else None,
        )
        self._append_history(result)
        self._log(result)
        self._notifier.emit("action_deduplicated", {"tool": ctx.tool})
        return result

    def _dry_run(self, ctx: CallContext) -> ActionResult:
        """Finish a test-mode call. Chaos faults fire here too, so resilience
        can be exercised without running the tool."""
        try:
            corrupt = self._inject_chaos(ctx)
            output: Any = (
                f"[TEST MODE] Would route to fallback: {ctx.tool}" if ctx.is_fallback
                else f"[TEST MODE] Would execute: {ctx.action.description}"
            )
            if corrupt:
                output = ChaosEngine.corrupt(output)
            result = self._make_result(ctx, output=output, cost=ctx.cost, reversible=None)
        except ToolTimeoutError as e:
            result = self._tool_failed(ctx, e, timed_out=True)
        except Exception as e:
            result = self._tool_failed(ctx, e)
        self._append_history(result)
        self._log(result)
        return result

    def _run_fallback(self, primary: CallContext, route: Any) -> ActionResult:
        """Run a fallback route through the same guards as a primary call."""
        ctx = CallContext(
            shield=self,
            action=primary.action,
            tool=route.fallback_tool,
            cost=route.fallback_cost,
            args=primary.args,
            kwargs=primary.kwargs,
            risk_override=primary.risk_override,
            route=route,
        )
        early = self._enter_guards(ctx)
        if early is not None:
            return early
        self._fallback.record_fallback(primary.action.tool_name)
        return self._run_entered(ctx, route.fallback_fn, None)

    # -- Shared by the sync and async execution paths --

    def _chaos_fault(self, ctx: CallContext) -> Any:
        """The fault to inject, if chaos is allowed to fire in this mode."""
        chaos = self._chaos
        if not chaos or not chaos.enabled:
            return None
        if not (self.is_test_mode or chaos.allow_production):
            return None
        return chaos.should_inject(ctx.tool, ctx.action.args)

    def _inject_chaos(self, ctx: CallContext) -> bool:
        """Apply a fault. Returns True if the tool's output must be corrupted."""
        fault = self._chaos_fault(ctx)
        if fault is None:
            return False
        self._chaos.inject(fault, ctx.tool)
        return fault.fault_type is FaultType.CORRUPT_OUTPUT

    def _begin_execution(self, ctx: CallContext) -> None:
        attributes = {"tool.name": ctx.tool, "tool.cost": ctx.cost}
        if ctx.is_fallback:
            attributes["tool.fallback_for"] = ctx.action.tool_name
        else:
            attributes["tool.risk"] = ctx.action.risk.value
        ctx.span = self._start_span(f"tool.{ctx.tool}", attributes)
        ctx.t0 = time.time()

    def _tool_failed(
        self, ctx: CallContext, error: Exception, timed_out: bool = False
    ) -> ActionResult:
        """Turn a raised exception into the call's result."""
        if ctx.is_fallback:
            # A fallback that raised is not charged; its failure is reported
            # by _finish_call. Rate and quota stay consumed: it did run.
            ctx.charge = False
            return self._make_result(ctx, error=str(error))
        if self._circuit_breaker:
            self._circuit_breaker.record_failure(ctx.tool)
        self._notifier.emit("action_timeout" if timed_out else "action_error", {
            "tool": ctx.tool, "error": str(error),
        })
        return self._make_result(ctx, error=str(error), cost=ctx.cost)

    def _validated_result(self, ctx: CallContext, output: Any) -> ActionResult:
        """Run the output validator (if any) and build the result."""
        if self._output_validator:
            try:
                check = self._output_validator.validate(ctx.tool, output)
            except Exception as e:
                # A faulty validator must not leak reservations or crash the
                # caller. The body already ran, so its cost is real.
                if not ctx.is_fallback:
                    if self._circuit_breaker:
                        self._circuit_breaker.record_failure(ctx.tool)
                    self._notifier.emit("action_error", {
                        "tool": ctx.tool, "error": str(e),
                    })
                return self._make_result(
                    ctx, error=f"Output validation failed: {e}", cost=ctx.cost,
                    output_withheld=True,
                )
            if not check.passed:
                self._notifier.emit("output_blocked", {
                    "tool": ctx.tool, "violations": check.violations,
                })
                ctx.span_status = "output_blocked"
                return self._make_result(
                    ctx,
                    error=f"Output blocked: {'; '.join(check.violations)}",
                    cost=ctx.cost,
                    output_withheld=True,
                )
            output = check.filtered_output
        return self._make_result(
            ctx, output=output, cost=ctx.cost,
            reversible=self._tool_reversibility.get(ctx.tool),
        )

    def _apply_actual_cost(self, ctx: CallContext, result: ActionResult) -> None:
        """Let a tool report its real provider charge after execution."""
        fn = self._tool_actual_costs.get(ctx.tool)
        if fn is None or result.blocked or result.error is not None:
            return
        try:
            actual = float(fn(result.output))
        except Exception:
            return  # no usable report: the declared cost stands
        if actual >= 0:
            result.cost = actual
            result.actual_cost = actual
            result.cost_source = "actual"

    def _finish_call(self, ctx: CallContext, result: ActionResult) -> ActionResult:
        """Settle reservations and record the call. Used by every path."""
        self._apply_actual_cost(ctx, result)
        for guard in reversed(ctx.entered):
            try:
                guard.commit(ctx, result)
            except Exception as e:
                self._notifier.emit("guard_error", {
                    "tool": ctx.tool, "guard": guard.name, "error": str(e),
                })
        ctx.settled = True
        self._append_history(result)
        self._log(result)

        if ctx.is_fallback:
            if result.success:
                self._notifier.emit("action_fallback", {
                    "tool": ctx.action.tool_name, "fallback": ctx.tool,
                })
            elif ctx.span_status != "output_blocked":
                self._notifier.emit("action_fallback_failed", {
                    "tool": ctx.action.tool_name,
                    "fallback": ctx.tool,
                    "error": result.error,
                })
        elif result.success:
            if self._circuit_breaker:
                self._circuit_breaker.record_success(ctx.tool)
            self._notifier.emit("action_completed", {
                "tool": ctx.tool, "cost": result.cost,
            })

        # The tool already ran: an outage while reading totals must not turn
        # its result into an exception.
        try:
            if self._budget.warning:
                self._notifier.emit("budget_warning", {
                    "spent": self._budget.spent, "budget": self._budget.budget,
                })
        except StateBackendError as e:
            self._backend_error(e)

        duration = time.time() - ctx.t0
        # The estimator and anomaly detector are side-channels: a faulty one
        # must never crash the tool call it is observing.
        try:
            self._estimator.record(
                ctx.tool, cost=result.cost, duration=duration,
                success=result.success,
            )
        except Exception:
            pass
        if self._anomaly is not None and result.success:
            try:
                self._emit_anomalies(ctx.tool, self._anomaly.record(ctx.tool, result.cost))
            except Exception:
                pass

        attributes: dict[str, Any] = {
            "tool.duration_ms": duration * 1000,
            "tool.blocked": result.blocked,
        }
        if result.error:
            attributes["error.message"] = result.error
        if ctx.span_status:
            attributes["output.blocked"] = True
        status = ctx.span_status or ("error" if result.error else "ok")
        self._end_span(ctx.span, status, attributes)
        return result

    def _emit_anomalies(self, tool: str, alerts: Any) -> None:
        for alert in alerts or []:
            self._notifier.emit("anomaly_detected", {
                "tool": tool, "severity": alert.severity, "message": alert.message,
            })

    def _abort_call(self, ctx: CallContext, error: BaseException, message: str) -> None:
        """Record an interruption and release what the call held. The caller
        re-raises. Rate, quota and budget go back; an idempotency marker stays
        if the body may have run."""
        if ctx.settled:
            return
        self._end_span(ctx.span, "error", {"error.message": message})
        result = self._make_result(ctx, error=message)
        self._append_history(result)
        self._log(result)
        self._unwind(ctx)
        if ctx.is_fallback:
            self._notifier.emit("action_fallback_failed", {
                "tool": ctx.action.tool_name, "fallback": ctx.tool, "error": message,
            })

    def _interrupted_message(self, ctx: CallContext, error: BaseException) -> str:
        if ctx.is_fallback:
            return f"Fallback interrupted: {error}"
        return f"Interrupted: {type(error).__name__}: {error}"

    # -- Sync execution --

    def execute(
        self,
        action: Action,
        func: Callable,
        args: tuple = (),
        kwargs: dict | None = None,
        cost: float = 0.0,
        risk_override: RiskLevel | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> ActionResult:
        """Public execution entry point for framework adapters.

        Integrations should call this instead of the private ``_execute``
        method. It runs the full safety pipeline and returns an ActionResult.
        """
        return self._execute(
            action, func, args, kwargs or {}, cost, risk_override, retry_policy
        )

    def _execute(
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
        early = self._enter_guards(ctx)
        if early is not None:
            return early
        return self._run_entered(ctx, func, retry_policy)

    def _invoke_tool(self, ctx: CallContext, func: Callable) -> Any:
        if ctx.is_fallback and inspect.iscoroutinefunction(func):
            # Async fallback: drive the coroutine with the tool's timeout.
            deadline = (
                self._timeout.policy.get_timeout(ctx.tool) if self._timeout else None
            )
            return _resolve_maybe_async(func(*ctx.args, **ctx.kwargs), timeout=deadline)
        if self._timeout:
            out = self._timeout.execute(
                ctx.tool, func, ctx.args, ctx.kwargs, inflight=ctx.inflight
            )
        else:
            out = func(*ctx.args, **ctx.kwargs)
        if ctx.is_fallback:
            out = _resolve_maybe_async(out)
        if inspect.isawaitable(out):
            # It will never be awaited here. Fail loudly, don't report success.
            close = getattr(out, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
            raise TypeError(
                f"Tool '{ctx.tool}' is async; wrap it with "
                f"AsyncWarderBox.async_tool, not @shield.tool"
            )
        return out

    def _run_entered(
        self, ctx: CallContext, func: Callable, retry_policy: RetryPolicy | None
    ) -> ActionResult:
        self._begin_execution(ctx)
        try:
            try:
                corrupt = self._inject_chaos(ctx)
                ctx.started = True
                # Protected tools must not auto-retry: a retry after a side
                # effect landed is exactly the double-send being guarded against.
                if retry_policy and not ctx.protected:
                    if not retry_policy.overlap_on_timeout:
                        ctx.inflight = InFlight()
                    output, _errors = execute_with_retry(
                        lambda: self._invoke_tool(ctx, func), (), {}, retry_policy
                    )
                else:
                    output = self._invoke_tool(ctx, func)
                if corrupt:
                    output = ChaosEngine.corrupt(output)
                result = self._validated_result(ctx, output)
            except ToolTimeoutError as e:
                result = self._tool_failed(ctx, e, timed_out=True)
            except Exception as e:
                result = self._tool_failed(ctx, e)
            return self._finish_call(ctx, result)
        except BaseException as e:
            self._abort_call(ctx, e, self._interrupted_message(ctx, e))
            raise

    # -- Cost Reconciliation --

    def reconcile_cost(self, action_id: str, actual_cost: float) -> bool:
        """Update a recorded action with the actual provider charge.

        Updates the budget, the history, the estimator and the anomaly detector.
        The audit log is append-only and keeps the cost known at call time; the
        change is announced through the ``cost_reconciled`` event.
        """
        entry = self._budget.reconcile(action_id, actual_cost)
        if entry is None:
            return False
        recorded = entry.estimated_cost
        with self._history_lock:
            target = next(
                (r for r in reversed(self._history) if r.action.id == action_id),
                None,
            )
            if target is not None:
                target.cost = actual_cost
                target.actual_cost = actual_cost
                target.cost_source = "reconciled"
        tool = entry.tool_name
        try:
            self._estimator.reconcile(tool, recorded, actual_cost)
        except Exception:
            pass
        if self._anomaly is not None:
            try:
                self._emit_anomalies(
                    tool, self._anomaly.reconcile(tool, recorded, actual_cost)
                )
            except Exception:
                pass
        self._notifier.emit("cost_reconciled", {
            "tool": tool, "action_id": action_id,
            "recorded": recorded, "actual": actual_cost,
        })
        return True

    def verify_audit(self, expected_head: dict | None = None) -> bool:
        """Verify the audit log's hash chain (when a DB logger is configured).

        See :mod:`warderbox.logger` for what this does and does not prove.
        """
        if self._logger is None:
            return True
        return self._logger.verify_integrity(expected_head)

    def adopt_unkeyed_audit_history(self) -> None:
        """Accept rows written before an audit key was configured. See
        :meth:`warderbox.logger.ActionLogger.adopt_unkeyed_history`."""
        if self._logger is None:
            raise ValueError("No audit log configured (db_path)")
        self._logger.adopt_unkeyed_history()

    def audit_head(self) -> dict | None:
        """The audit chain's position, to store outside the agent's reach."""
        return self._logger.audit_head() if self._logger else None

    # -- Rollback --

    def rollback(self, action_id: str | None = None) -> bool:
        # Walk backwards through unrestored snapshots so repeated rollback
        # calls undo one action at a time instead of restoring the same
        # snapshot forever.
        snap = None
        for candidate in reversed(self._snapshots.list_all()):
            if candidate.restored:
                continue
            if action_id is None or candidate.action_id == action_id:
                snap = candidate
                break

        if snap is None:
            return False

        tool_name = snap.tool_name
        if tool_name not in self._tool_states:
            return False

        restore_fn = self._tool_states[tool_name]["restore"]
        restore_fn(snap.state)
        snap.restored = True
        return True

    # -- History --

    def _masked_text(self, text: str | None) -> str | None:
        if not text or not self._masker:
            return text
        try:
            return self._masker.mask_string(text)
        except Exception:
            return "[UNMASKABLE]"

    def history(self, raw: bool = False) -> list[dict]:
        """Recent calls, with errors and block reasons masked like the audit log.

        ``raw=True`` returns them unmasked, for code in the same process that
        needs the original text. The dashboard never uses it.
        """
        with self._history_lock:
            snapshot = list(self._history)
        text = (lambda value: value) if raw else self._masked_text
        results = []
        for r in snapshot:
            results.append({
                "id": r.action.id,
                "tool": r.action.tool_name,
                "risk": r.action.risk.value,
                "blocked": r.blocked,
                "block_reason": text(r.block_reason),
                "error": text(r.error),
                "cost": r.cost,
                "cost_source": r.cost_source,
                "success": r.success,
                "has_snapshot": r.snapshot_id is not None,
                "reversible": r.reversible,
                "fallback": r.fallback_tool,
                "timestamp": r.action.timestamp,
            })
        return results

    def last_result(self) -> ActionResult | None:
        with self._history_lock:
            return self._history[-1] if self._history else None

    def load_history_from_db(self, db_path: str | None = None) -> int:
        """Rebuild the in-memory history view from a persisted audit log.

        Lets a fresh instance (e.g. the CLI dashboard) display actions that
        were recorded in a previous process. Returns the number of entries
        loaded.
        """
        logger = self._logger
        close_after = False
        if logger is None:
            if db_path is None:
                return 0
            logger = ActionLogger(db_path)
            close_after = True

        rows = logger.query(limit=1_000_000)
        rebuilt: list[ActionResult] = []
        for row in reversed(rows):  # query() returns newest-first
            try:
                args = json.loads(row["args"]) if row["args"] else {}
            except (TypeError, ValueError):
                args = {}
            try:
                risk = RiskLevel(row["risk"]) if row["risk"] else RiskLevel.LOW
            except ValueError:
                risk = RiskLevel.LOW
            action = Action(
                tool_name=row["tool_name"],
                args=args if isinstance(args, dict) else {"value": args},
                id=row["id"],
                timestamp=row["timestamp"],
                risk=risk,
                cost=row["cost"] or 0.0,
            )
            output = row["output"]
            if output is not None:
                try:
                    output = json.loads(output)
                except (TypeError, ValueError):
                    pass
            rebuilt.append(ActionResult(
                action=action,
                output=output,
                error=row["error"],
                blocked=bool(row["blocked"]),
                block_reason=row["block_reason"],
                snapshot_id=row["snapshot_id"],
                cost=row["cost"] or 0.0,
                fallback_tool=dict(row).get("fallback_tool"),
                reversible=(
                    None if dict(row).get("reversible") is None
                    else bool(dict(row)["reversible"])
                ),
            ))

        with self._history_lock:
            self._history = deque(rebuilt, maxlen=self._max_history)
        # Rebuild spend so budget / FinOps reflect the persisted history.
        # reset() first makes repeated loads idempotent (no double counting).
        # Shared totals already include this spend and belong to all workers:
        # a dashboard loading history must never reset or re-count them.
        if not self._budget.shared:
            self._budget.reset()
            for r in rebuilt:
                if not r.blocked and r.cost:
                    self._budget.record(r.action.id, r.action.tool_name, r.cost)
        if close_after:
            logger.close()
        return len(rebuilt)

    # -- Stats --

    def stats(self) -> dict:
        with self._history_lock:
            snapshot = list(self._history)
        total = len(snapshot)
        blocked = sum(1 for r in snapshot if r.blocked)
        errors = sum(1 for r in snapshot if r.error)
        budget = self._budget.summary()
        rate_limits = self._rate_limiter.stats()
        return {
            "degraded": bool(budget.get("degraded") or rate_limits.get("degraded")),
            "total_actions": total,
            "blocked": blocked,
            "errors": errors,
            "successful": total - blocked - errors,
            "snapshots": self._snapshots.count,
            "budget": budget,
            "rate_limits": rate_limits,
            "audit_log_errors": self._audit_log_errors,
        }
