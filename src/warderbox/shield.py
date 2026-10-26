"""WarderBox — the main class that ties together rollback, cost control, and testing."""

from __future__ import annotations

import functools
import inspect
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Any

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.approval import ApprovalManager
from warderbox.budget import BudgetTracker
from warderbox.capabilities import CapabilityManager
from warderbox.chaos import ChaosEngine
from warderbox.circuitbreaker import CircuitBreaker
from warderbox.config import parse_config
from warderbox.estimator import CostEstimator
from warderbox.fallback import FallbackManager
from warderbox.idempotency import DedupeResult, IdempotencyGuard
from warderbox.logger import ActionLogger
from warderbox.masking import DataMasker
from warderbox.output_validation import OutputValidator
from warderbox.quota import QuotaManager
from warderbox.ratelimit import RateLimiter, ToolLimit
from warderbox.retry import RetryPolicy, execute_with_retry
from warderbox.rules import RuleEngine
from warderbox.snapshot import SnapshotStore
from warderbox.timeout import TimeoutError as ToolTimeoutError
from warderbox.timeout import TimeoutExecutor
from warderbox.tracing import Tracer
from warderbox.webhooks import EventNotifier


class Mode(Enum):
    PRODUCTION = "production"
    TEST = "test"


OnBlockCallback = Callable[[Action, list[str]], bool]

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def _resolve_maybe_async(value: Any, timeout: float | None = None) -> Any:
    """Run an awaitable to completion when no event loop is running.

    Fallback functions may be async; when a fallback is executed from a
    worker thread (the async pipeline runs _preflight via asyncio.to_thread)
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
    ) -> None:
        self._mode = Mode(mode)
        self._agent_name = agent_name
        self._auto_snapshot = auto_snapshot
        self._on_block = on_block
        self._rules = RuleEngine()
        self._budget = BudgetTracker(budget=budget)
        self._snapshots = SnapshotStore(max_snapshots=max_snapshots)
        self._max_history = max_history
        self._history: deque[ActionResult] = deque(maxlen=max_history)
        self._history_lock = threading.Lock()
        self._tool_states: dict[str, Any] = {}
        self._tool_reversibility: dict[str, bool | None] = {}
        self._logger = ActionLogger(db_path, masker=None) if db_path else None
        self._rate_limiter = RateLimiter(
            global_rpm=global_rpm,
            global_rph=global_rph,
            tool_limits=tool_limits or {},
        )
        self._notifier = EventNotifier(allow_private_hosts=allow_private_webhooks)
        self._masker: DataMasker | None = None
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

    def set_timeout(self, timeout: TimeoutExecutor) -> None:
        self._timeout = timeout

    def set_idempotency(self, guard: IdempotencyGuard) -> None:
        self._idempotency = guard

    def set_output_validator(self, validator: OutputValidator) -> None:
        self._output_validator = validator

    def set_fallback(self, fallback: FallbackManager) -> None:
        self._fallback = fallback

    def set_quota(self, quota: QuotaManager) -> None:
        self._quota = quota

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
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            tool_name = func.__name__

            if inspect.iscoroutinefunction(func):
                import warnings
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
        """Write to the audit logger, isolating logger failures from the call."""
        if not self._logger:
            return
        try:
            self._logger.log(result)
        except Exception:
            pass

    def _release_reservations(self, action: Action, reservation: Any) -> None:
        """Release budget/rate/quota and any in-flight idempotency marker.

        Used to clean up when an error or interruption happens after reserving
        but before the call is finalized, so reservations never leak.
        """
        if reservation is not None:
            self._budget.release(reservation.reservation_id)
        self._rate_limiter.release(action.tool_name)
        if self._quota:
            self._quota.release(action.tool_name)
        if self._idempotency and self._idempotency.is_protected(action.tool_name):
            self._idempotency.fail(action.tool_name, action.args, keep=False)

    def _consult_on_block(self, action: Action, reasons: list[str]) -> bool:
        """Call the on_block callback defensively. A raising callback is
        treated as a denial (fail closed) and never crashes the call."""
        try:
            return bool(self._on_block(action, reasons))
        except Exception:
            return False

    def _blocked(
        self, action: Action, reason: str, event: str | None
    ) -> ActionResult:
        result = ActionResult(action=action, blocked=True, block_reason=reason)
        self._append_history(result)
        self._log(result)
        if event is not None:
            self._notifier.emit(event, {"tool": action.tool_name, "reason": reason})
        return result

    def _fallback_error_result(
        self,
        action: Action,
        fallback_tool: str,
        message: str,
        reservation: Any,
        protected: bool,
        snapshot_id: str | None,
        span: Any,
        *,
        executed: bool,
        release_limits: bool = False,
        keep_marker: bool = True,
        cost: float = 0.0,
    ) -> ActionResult:
        """Finish a failed fallback without leaking reservations or crashing.

        ``executed`` means the fallback body returned (so its cost is real);
        when it is False the budget reservation is released. Rate/quota are
        released only when ``release_limits`` is set (pre-execution failures).
        """
        if reservation is not None:
            if executed:
                try:
                    self._budget.commit(reservation.reservation_id)
                except Exception:
                    self._budget.release(reservation.reservation_id)
            else:
                self._budget.release(reservation.reservation_id)
        if release_limits:
            self._rate_limiter.release(fallback_tool)
            if self._quota:
                self._quota.release(fallback_tool)
        if protected and self._idempotency:
            try:
                self._idempotency.fail(
                    fallback_tool, action.args, keep=keep_marker
                )
            except Exception:
                pass
        result = ActionResult(
            action=action,
            error=message,
            cost=cost,
            snapshot_id=snapshot_id,
            fallback_tool=fallback_tool,
            reversible=self._tool_reversibility.get(fallback_tool),
        )
        self._append_history(result)
        self._log(result)
        self._notifier.emit("action_fallback_failed", {
            "tool": action.tool_name,
            "fallback": fallback_tool,
            "error": message,
        })
        self._end_span(span, "error", {"error.message": message})
        return result

    def _run_fallback(
        self, action: Action, route: Any, args: tuple, kwargs: dict,
        risk_override: RiskLevel | None = None,
    ) -> ActionResult:
        """Run a fallback route through the same guard gates as a primary call."""
        fallback_tool = route.fallback_tool

        # Capability: the fallback *tool* must itself be permitted — being
        # allowed to call the primary does not grant the fallback.
        if self._capabilities:
            cap_ok, cap_reason = self._capabilities.check(
                self._agent_name, fallback_tool, args=action.args,
                cost=route.fallback_cost,
            )
            if not cap_ok:
                return self._blocked(
                    action, f"Capability denied (fallback): {cap_reason}",
                    "action_blocked",
                )

        detected_risk, reasons = self._rules.evaluate(action)
        # Honor the per-tool risk floor: an explicit risk= can only raise.
        if risk_override is not None:
            detected_risk = max(
                detected_risk, risk_override, key=lambda r: _RISK_ORDER[r]
            )
            if not reasons:
                reasons = [f"declared risk {risk_override.value}"]
        if detected_risk == RiskLevel.CRITICAL:
            return self._blocked(
                action,
                f"CRITICAL risk blocked (fallback): {'; '.join(reasons)}",
                "action_blocked",
            )
        action.risk = detected_risk

        # on_block: a HIGH-risk action routed to a fallback must still consult
        # the user callback, exactly like the primary pipeline.
        if detected_risk == RiskLevel.HIGH and self._on_block:
            if not self._consult_on_block(action, reasons):
                return self._blocked(
                    action,
                    f"Blocked by user (fallback): {'; '.join(reasons)}",
                    "action_blocked",
                )

        # Approval: request approval for whichever tool actually carries the
        # policy (primary preferred), so a policy on the fallback tool is not
        # bypassed by requesting approval for an unpoliced primary.
        if self._approval:
            approval_tool = None
            if self._approval.needs_approval(action.tool_name, detected_risk.value):
                approval_tool = action.tool_name
            elif self._approval.needs_approval(fallback_tool, detected_risk.value):
                approval_tool = fallback_tool
            if approval_tool is not None:
                decision = self._approval.request_approval(
                    approval_tool, args=action.args,
                    risk_level=detected_risk.value, action_id=action.id,
                )
                if not decision.approved:
                    return self._blocked(
                        action,
                        f"Approval {decision.decision.value} (fallback): "
                        f"{decision.reason or 'not approved'}",
                        "approval_denied",
                    )

        if self.is_test_mode:
            # Dry-run: simulate budget/rate/quota (reserve then release) so a
            # dry-run still reports a fallback that would exceed a limit, but
            # never invoke the fallback or consume real state.
            sim = self._budget.reserve(action.id, fallback_tool, route.fallback_cost)
            if sim is None:
                return self._blocked(
                    action,
                    f"Fallback budget exceeded (cost: ${route.fallback_cost:.2f})",
                    None,
                )
            rate_ok, rate_reason = self._rate_limiter.reserve(fallback_tool)
            if not rate_ok:
                self._budget.release(sim.reservation_id)
                return self._blocked(action, rate_reason, "rate_limited")
            if self._quota:
                q_ok, q_reason = self._quota.reserve(fallback_tool)
                if not q_ok:
                    self._budget.release(sim.reservation_id)
                    self._rate_limiter.release(fallback_tool)
                    return self._blocked(action, q_reason, "quota_exceeded")
                self._quota.release(fallback_tool)
            self._budget.release(sim.reservation_id)
            self._rate_limiter.release(fallback_tool)
            result = ActionResult(
                action=action,
                output=f"[TEST MODE] Would route to fallback: {fallback_tool}",
                cost=route.fallback_cost,
                fallback_tool=fallback_tool,
            )
            self._append_history(result)
            self._log(result)
            return result

        reservation = self._budget.reserve(
            action.id, fallback_tool, route.fallback_cost
        )
        if reservation is None:
            return self._blocked(
                action,
                f"Fallback budget exceeded (cost: ${route.fallback_cost:.2f}, "
                f"spent: ${self._budget.spent:.2f}, budget: ${self._budget.budget:.2f})",
                "budget_exceeded",
            )

        try:
            rate_ok, rate_reason = self._rate_limiter.reserve(fallback_tool)
        except Exception as e:
            return self._fallback_error_result(
                action, fallback_tool, f"Rate limiter failed: {e}",
                reservation, False, None, None,
                executed=False, release_limits=True, keep_marker=False,
            )
        except BaseException:
            self._budget.release(reservation.reservation_id)
            raise
        if not rate_ok:
            self._budget.release(reservation.reservation_id)
            return self._blocked(action, rate_reason, "rate_limited")

        if self._quota:
            try:
                q_ok, q_reason = self._quota.reserve(fallback_tool)
            except Exception as e:
                return self._fallback_error_result(
                    action, fallback_tool, f"Quota reservation failed: {e}",
                    reservation, False, None, None,
                    executed=False, release_limits=True, keep_marker=False,
                )
            except BaseException:
                self._budget.release(reservation.reservation_id)
                self._rate_limiter.release(fallback_tool)
                raise
            if not q_ok:
                self._budget.release(reservation.reservation_id)
                self._rate_limiter.release(fallback_tool)
                return self._blocked(action, q_reason, "quota_exceeded")

        # Idempotency: a protected fallback tool must dedupe too.
        protected = bool(
            self._idempotency and self._idempotency.is_protected(fallback_tool)
        )
        if protected:
            try:
                status, cached = self._idempotency.begin(
                    fallback_tool, action.args
                )
            except Exception as e:
                return self._fallback_error_result(
                    action, fallback_tool,
                    f"Idempotency backend failed: {e}",
                    reservation, True, None, None,
                    executed=False, release_limits=True, keep_marker=False,
                )
            except BaseException as e:
                self._fallback_error_result(
                    action, fallback_tool,
                    f"Fallback interrupted: {e}",
                    reservation, True, None, None,
                    executed=False, release_limits=True, keep_marker=False,
                )
                raise
            if status == DedupeResult.DUPLICATE:
                self._budget.release(reservation.reservation_id)
                self._rate_limiter.release(fallback_tool)
                if self._quota:
                    self._quota.release(fallback_tool)
                result = ActionResult(
                    action=action, blocked=True,
                    block_reason="Duplicate call blocked (cached result available)",
                    output=cached,
                    fallback_tool=fallback_tool,
                )
                self._append_history(result)
                self._log(result)
                self._notifier.emit("action_deduplicated", {"tool": fallback_tool})
                return result

        # Snapshot the fallback tool's state (if it registered capture/restore)
        # before execution, so a fallback action can be rolled back too.
        snapshot_id = None
        if self._auto_snapshot and fallback_tool in self._tool_states:
            try:
                state = self._tool_states[fallback_tool]["capture"]()
                snap = self._snapshots.capture(
                    action.id, fallback_tool, state
                )
                snapshot_id = snap.id
            except Exception as e:
                return self._fallback_error_result(
                    action, fallback_tool, f"Fallback snapshot failed: {e}",
                    reservation, protected, None, None,
                    executed=False, release_limits=True, keep_marker=False,
                )
            except BaseException as e:
                self._fallback_error_result(
                    action, fallback_tool, f"Fallback interrupted: {e}",
                    reservation, protected, None, None,
                    executed=False, release_limits=True, keep_marker=False,
                )
                raise

        self._fallback.record_fallback(action.tool_name)
        fb_span = self._start_span(
            f"tool.{fallback_tool}",
            {"tool.name": fallback_tool,
             "tool.cost": route.fallback_cost,
             "tool.fallback_for": action.tool_name},
        )
        timeout_s = (
            self._timeout.policy.get_timeout(fallback_tool)
            if self._timeout else None
        )
        fb_t0 = time.time()
        try:
            if inspect.iscoroutinefunction(route.fallback_fn):
                # Async fallback: drive the coroutine with the tool timeout.
                output = _resolve_maybe_async(
                    route.fallback_fn(*args, **kwargs), timeout=timeout_s
                )
            elif self._timeout:
                output = self._timeout.execute(
                    fallback_tool, route.fallback_fn, args, kwargs
                )
            else:
                output = _resolve_maybe_async(route.fallback_fn(*args, **kwargs))
        except Exception as e:
            return self._fallback_error_result(
                action, fallback_tool, str(e),
                reservation, protected, snapshot_id, fb_span,
                executed=False, release_limits=False, keep_marker=True,
            )
        except BaseException as e:
            self._fallback_error_result(
                action, fallback_tool, f"Fallback interrupted: {e}",
                reservation, protected, snapshot_id, fb_span,
                executed=False, release_limits=True, keep_marker=True,
            )
            raise

        if self._output_validator:
            try:
                check = self._output_validator.validate(fallback_tool, output)
            except Exception as e:
                # A faulty custom validator must not leak reservations or crash
                # the caller; the fallback body already ran, so charge its cost.
                return self._fallback_error_result(
                    action, fallback_tool, f"Output validation failed: {e}",
                    reservation, protected, snapshot_id, fb_span,
                    executed=True, release_limits=False, keep_marker=True,
                    cost=route.fallback_cost,
                )
            except BaseException as e:
                self._fallback_error_result(
                    action, fallback_tool, f"Output validation interrupted: {e}",
                    reservation, protected, snapshot_id, fb_span,
                    executed=True, release_limits=True, keep_marker=True,
                    cost=route.fallback_cost,
                )
                raise
            if not check.passed:
                self._budget.commit(reservation.reservation_id)
                if protected:
                    self._idempotency.fail(fallback_tool, action.args)
                result = ActionResult(
                    action=action,
                    error=f"Output blocked: {'; '.join(check.violations)}",
                    cost=route.fallback_cost,
                    snapshot_id=snapshot_id,
                    fallback_tool=fallback_tool,
                    reversible=self._tool_reversibility.get(fallback_tool),
                )
                self._append_history(result)
                self._log(result)
                self._notifier.emit("output_blocked", {
                    "tool": fallback_tool,
                    "violations": check.violations,
                })
                self._end_span(fb_span, "output_blocked", {"output.blocked": True})
                return result
            output = check.filtered_output

        result = ActionResult(
            action=action,
            output=output,
            cost=route.fallback_cost,
            snapshot_id=snapshot_id,
            fallback_tool=fallback_tool,
            reversible=self._tool_reversibility.get(fallback_tool),
        )
        self._budget.commit(reservation.reservation_id)
        if protected:
            try:
                self._idempotency.complete(fallback_tool, action.args, output)
            except Exception as e:
                self._notifier.emit("idempotency_error", {
                    "tool": fallback_tool, "error": str(e),
                })
        self._append_history(result)
        self._log(result)
        # First-class recording: the fallback execution feeds the estimator and
        # anomaly detector under the fallback tool name, like any other call.
        duration = time.time() - fb_t0
        try:
            self._estimator.record(
                fallback_tool, cost=route.fallback_cost, duration=duration, success=True,
            )
        except Exception:
            pass
        if self._anomaly is not None:
            try:
                for alert in self._anomaly.record(fallback_tool, route.fallback_cost) or []:
                    self._notifier.emit("anomaly_detected", {
                        "tool": fallback_tool,
                        "severity": alert.severity,
                        "message": alert.message,
                    })
            except Exception:
                pass
        if self._budget.warning:
            self._notifier.emit("budget_warning", {
                "spent": self._budget.spent, "budget": self._budget.budget,
            })
        self._notifier.emit("action_fallback", {
            "tool": action.tool_name,
            "fallback": fallback_tool,
        })
        self._end_span(fb_span, "ok", {"tool.duration_ms": duration * 1000})
        return result

    def _preflight(
        self,
        action: Action,
        cost: float,
        risk_override: RiskLevel | None,
        args: tuple,
        kwargs: dict,
    ) -> tuple[ActionResult | None, Any, str | None]:
        """Run every pre-execution guard shared by the sync and async paths.

        Returns ``(early_result, reservation, snapshot_id)``. When
        ``early_result`` is not None the caller must return it unchanged
        (blocked, deduplicated, fallback, or test-mode result). Otherwise
        execution should proceed using the returned reservation/snapshot.
        """
        if self._capabilities:
            cap_ok, cap_reason = self._capabilities.check(
                self._agent_name, action.tool_name,
                args=action.args, cost=cost,
            )
            if not cap_ok:
                return self._blocked(
                    action, f"Capability denied: {cap_reason}", "action_blocked",
                ), None, None

        if self._idempotency:
            dedup_status, cached = self._idempotency.check(
                action.tool_name, action.args,
            )
            if dedup_status == DedupeResult.DUPLICATE:
                result = ActionResult(
                    action=action, blocked=True,
                    block_reason="Duplicate call blocked (cached result available)",
                    output=cached,
                )
                self._append_history(result)
                self._log(result)
                self._notifier.emit("action_deduplicated", {
                    "tool": action.tool_name,
                })
                return result, None, None

        if self._circuit_breaker:
            cb_ok, cb_reason = self._circuit_breaker.should_allow(action.tool_name)
            if not cb_ok:
                if self._fallback and self._fallback.has_fallback(action.tool_name):
                    route = self._fallback.get_fallback(
                        action.tool_name, reason="circuit_breaker",
                    )
                    if route and route.fallback_fn:
                        fb_result = self._run_fallback(
                            action, route, args, kwargs, risk_override
                        )
                        if fb_result is not None:
                            return fb_result, None, None

                return self._blocked(
                    action, f"Circuit breaker: {cb_reason}", "action_blocked",
                ), None, None

        detected_risk, reasons = self._rules.evaluate(action)
        # The per-tool `risk=` acts as a floor, never a ceiling: an explicit
        # override can only *raise* the risk, so declaring a tool LOW can never
        # suppress a CRITICAL pattern (e.g. DROP TABLE) detected in its args.
        if risk_override is not None:
            action.risk = max(
                detected_risk, risk_override, key=lambda r: _RISK_ORDER[r]
            )
        else:
            action.risk = detected_risk

        if action.risk == RiskLevel.CRITICAL:
            return self._blocked(
                action,
                f"CRITICAL risk blocked: {'; '.join(reasons)}",
                "action_blocked",
            ), None, None

        if action.risk == RiskLevel.HIGH and self._on_block:
            if not self._consult_on_block(action, reasons):
                return self._blocked(
                    action,
                    f"Blocked by user: {'; '.join(reasons)}",
                    "action_blocked",
                ), None, None

        if self._approval and self._approval.needs_approval(
            action.tool_name, action.risk.value
        ):
            decision = self._approval.request_approval(
                action.tool_name,
                args=action.args,
                risk_level=action.risk.value,
                action_id=action.id,
            )
            if not decision.approved:
                return self._blocked(
                    action,
                    f"Approval {decision.decision.value}: "
                    f"{decision.reason or 'not approved'}",
                    "approval_denied",
                ), None, None

        reservation = self._budget.reserve(action.id, action.tool_name, cost)
        if reservation is None:
            self._notifier.emit("budget_exceeded", {
                "tool": action.tool_name, "spent": self._budget.spent,
                "budget": self._budget.budget,
            })
            return self._blocked(
                action,
                f"Budget exceeded (spent: ${self._budget.spent:.2f}, "
                f"budget: ${self._budget.budget:.2f}, action cost: ${cost:.2f})",
                None,
            ), None, None

        try:
            rate_ok, rate_reason = self._rate_limiter.reserve(action.tool_name)
        except BaseException:
            self._budget.release(reservation.reservation_id)
            raise
        if not rate_ok:
            self._budget.release(reservation.reservation_id)
            return self._blocked(action, rate_reason, "rate_limited"), None, None

        if self._quota:
            try:
                q_ok, q_reason = self._quota.reserve(action.tool_name)
            except BaseException:
                self._budget.release(reservation.reservation_id)
                self._rate_limiter.release(action.tool_name)
                raise
            if not q_ok:
                self._budget.release(reservation.reservation_id)
                self._rate_limiter.release(action.tool_name)
                return self._blocked(action, q_reason, "quota_exceeded"), None, None

        try:
            return self._preflight_tail(action, cost, reservation)
        except BaseException:
            # Snapshot capture / idempotency begin raised (or the thread was
            # interrupted): release reservations so nothing leaks.
            self._release_reservations(action, reservation)
            raise

    def _preflight_tail(
        self, action: Action, cost: float, reservation: Any
    ) -> tuple[ActionResult | None, Any, str | None]:
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
            # Dry-run: don't consume real budget/rate/quota state, just
            # release the reservations so repeated simulations aren't throttled.
            self._budget.release(reservation.reservation_id)
            self._rate_limiter.release(action.tool_name)
            if self._quota:
                self._quota.release(action.tool_name)
            self._append_history(result)
            self._log(result)
            return result, None, None

        # Atomically reserve the idempotency slot right before execution so
        # concurrent calls / caller retries cannot double-fire a side effect.
        if self._idempotency and self._idempotency.is_protected(action.tool_name):
            status, cached = self._idempotency.begin(action.tool_name, action.args)
            if status == DedupeResult.DUPLICATE:
                self._budget.release(reservation.reservation_id)
                self._rate_limiter.release(action.tool_name)
                if self._quota:
                    self._quota.release(action.tool_name)
                result = ActionResult(
                    action=action, blocked=True,
                    block_reason="Duplicate call blocked (cached result available)",
                    output=cached,
                )
                self._append_history(result)
                self._log(result)
                self._notifier.emit("action_deduplicated", {"tool": action.tool_name})
                return result, None, None

        return None, reservation, snapshot_id

    def _finalize(
        self,
        action: Action,
        result: ActionResult,
        reservation: Any,
        snapshot_id: str | None,
        t0: float,
        span: Any = None,
    ) -> ActionResult:
        """Commit cost, record usage and side-channels after execution.

        Shared by the sync and async execution paths.
        """
        self._budget.commit(reservation.reservation_id)
        # Rate limit and quota were already counted atomically at reservation
        # time (_preflight), so we don't double-count here.
        self._append_history(result)
        self._log(result)

        if self._idempotency and self._idempotency.is_protected(action.tool_name):
            try:
                if result.success:
                    self._idempotency.complete(
                        action.tool_name, action.args, result.output,
                    )
                else:
                    # Keep the in-flight marker: a protected tool that failed may
                    # already have produced its side effect, so block retries.
                    self._idempotency.fail(action.tool_name, action.args)
            except Exception as e:
                # A backend failure must not turn a completed tool call into a
                # caller-visible exception.
                self._notifier.emit("idempotency_error", {
                    "tool": action.tool_name, "error": str(e),
                })

        if result.success:
            if self._circuit_breaker:
                self._circuit_breaker.record_success(action.tool_name)
            self._notifier.emit("action_completed", {
                "tool": action.tool_name, "cost": result.cost,
            })

        if self._budget.warning:
            self._notifier.emit("budget_warning", {
                "spent": self._budget.spent, "budget": self._budget.budget,
            })

        duration = time.time() - t0
        # Estimator is a side-channel: a faulty/custom estimator must never
        # crash the tool call it is measuring.
        try:
            self._estimator.record(
                action.tool_name,
                cost=result.cost,
                duration=duration,
                success=result.success,
            )
        except Exception:
            pass
        if self._anomaly is not None and result.success:
            # Anomaly detection is a side-channel: a faulty detector or alert
            # callback must never crash the tool call it is observing.
            try:
                alerts = self._anomaly.record(action.tool_name, result.cost)
                for alert in alerts or []:
                    self._notifier.emit("anomaly_detected", {
                        "tool": action.tool_name,
                        "severity": alert.severity,
                        "message": alert.message,
                    })
            except Exception:
                pass

        span_attrs = {
            "tool.duration_ms": duration * 1000,
            "tool.blocked": result.blocked,
        }
        if result.error:
            span_attrs["error.message"] = result.error
        self._end_span(span, "error" if result.error else "ok", span_attrs)

        return result

    def _apply_output_validation(
        self, action: Action, output: Any, snapshot_id: str | None, cost: float
    ) -> ActionResult:
        """Run the output validator (if any) and build the result."""
        if self._output_validator:
            check = self._output_validator.validate(action.tool_name, output)
            if not check.passed:
                self._notifier.emit("output_blocked", {
                    "tool": action.tool_name,
                    "violations": check.violations,
                })
                return ActionResult(
                    action=action,
                    error=f"Output blocked: {'; '.join(check.violations)}",
                    snapshot_id=snapshot_id,
                    cost=cost,
                )
            output = check.filtered_output
        return ActionResult(
            action=action,
            output=output,
            snapshot_id=snapshot_id,
            cost=cost,
            reversible=self._tool_reversibility.get(action.tool_name),
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
        early, reservation, snapshot_id = self._preflight(
            action, cost, risk_override, args, kwargs
        )
        if early is not None:
            return early

        try:
            return self._execute_body(
                action, func, args, kwargs, cost, retry_policy,
                reservation, snapshot_id,
            )
        except BaseException:
            # Interrupted (e.g. KeyboardInterrupt) before finalize: release
            # budget/rate/quota so nothing leaks. The idempotency marker is
            # kept (keep=True) because the tool body may already have run.
            self._budget.release(reservation.reservation_id)
            self._rate_limiter.release(action.tool_name)
            if self._quota:
                self._quota.release(action.tool_name)
            raise

    def _execute_body(
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
        try:
            return self._execute_body_impl(
                action, func, args, kwargs, cost, retry_policy,
                reservation, snapshot_id, span, t0,
            )
        except BaseException as e:
            # Record the interruption and close the span before unwinding.
            # _execute() releases the budget/rate/quota reservations.
            message = f"Interrupted: {type(e).__name__}: {e}"
            self._end_span(span, "error", {"error.message": message})
            result = ActionResult(
                action=action, error=message, snapshot_id=snapshot_id,
            )
            self._append_history(result)
            self._log(result)
            raise

    def _execute_body_impl(
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
    ) -> ActionResult:
        result = None
        if self._chaos and self._chaos.enabled:
            fault = self._chaos.should_inject(action.tool_name, action.args)
            if fault:
                try:
                    self._chaos.inject(fault, action.tool_name)
                except ToolTimeoutError as e:
                    if self._circuit_breaker:
                        self._circuit_breaker.record_failure(action.tool_name)
                    self._notifier.emit("action_timeout", {
                        "tool": action.tool_name, "error": str(e),
                    })
                    result = ActionResult(
                        action=action, error=str(e),
                        snapshot_id=snapshot_id, cost=cost,
                    )
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
            def _invoke() -> Any:
                if self._timeout:
                    out = self._timeout.execute(
                        action.tool_name, func, args, kwargs,
                    )
                else:
                    out = func(*args, **kwargs)
                if inspect.isawaitable(out):
                    # A sync tool returned a coroutine: it will never be
                    # awaited here. Fail loudly instead of reporting success.
                    try:
                        out.close()
                    except Exception:
                        pass
                    raise TypeError(
                        f"Tool '{action.tool_name}' is async; wrap it with "
                        f"AsyncWarderBox.async_tool, not @shield.tool"
                    )
                return out

            # Protected (idempotent-guarded) tools must not auto-retry: a retry
            # after a side effect already landed is exactly the double-send we
            # are guarding against.
            effective_retry = retry_policy
            if (
                retry_policy
                and self._idempotency
                and self._idempotency.is_protected(action.tool_name)
            ):
                effective_retry = None

            try:
                if effective_retry:
                    output, _errors = execute_with_retry(
                        _invoke, (), {}, effective_retry
                    )
                else:
                    output = _invoke()
                result = self._apply_output_validation(action, output, snapshot_id, cost)
            except ToolTimeoutError as e:
                if self._circuit_breaker:
                    self._circuit_breaker.record_failure(action.tool_name)
                self._notifier.emit("action_timeout", {
                    "tool": action.tool_name, "error": str(e),
                })
                result = ActionResult(
                    action=action, error=str(e),
                    snapshot_id=snapshot_id, cost=cost,
                )
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

        return self._finalize(action, result, reservation, snapshot_id, t0, span)

    # -- Cost Reconciliation --

    def reconcile_cost(self, action_id: str, actual_cost: float) -> bool:
        """Update a recorded action with the actual provider charge."""
        entry = self._budget.reconcile(action_id, actual_cost)
        return entry is not None

    # -- Rollback --

    def rollback(self, action_id: str | None = None) -> bool:
        if action_id:
            snap = self._snapshots.get_by_action(action_id)
        else:
            snap = self._snapshots.get_latest()

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

    def history(self) -> list[dict]:
        with self._history_lock:
            snapshot = list(self._history)
        results = []
        for r in snapshot:
            results.append({
                "id": r.action.id,
                "tool": r.action.tool_name,
                "risk": r.action.risk.value,
                "blocked": r.blocked,
                "block_reason": r.block_reason,
                "error": r.error,
                "cost": r.cost,
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
        return {
            "total_actions": total,
            "blocked": blocked,
            "errors": errors,
            "successful": total - blocked - errors,
            "snapshots": self._snapshots.count,
            "budget": self._budget.summary(),
            "rate_limits": self._rate_limiter.stats(),
        }
