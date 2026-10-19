"""WarderBox — the main class that ties together rollback, cost control, and testing."""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.approval import ApprovalManager
from warderbox.budget import BudgetTracker
from warderbox.capabilities import CapabilityManager
from warderbox.chaos import ChaosEngine
from warderbox.circuitbreaker import CircuitBreaker
from warderbox.config import parse_config, rules_from_config
from warderbox.fallback import FallbackManager
from warderbox.idempotency import IdempotencyGuard, DedupeResult
from warderbox.logger import ActionLogger
from warderbox.masking import DataMasker
from warderbox.output_validation import OutputValidator
from warderbox.quota import QuotaManager
from warderbox.ratelimit import RateLimiter, ToolLimit
from warderbox.rules import RuleEngine
from warderbox.snapshot import SnapshotStore
from warderbox.retry import RetryPolicy, execute_with_retry
from warderbox.timeout import TimeoutExecutor, TimeoutPolicy
from warderbox.timeout import TimeoutError as ToolTimeoutError
from warderbox.estimator import CostEstimator
from warderbox.tracing import Tracer
from warderbox.webhooks import EventNotifier


class Mode(Enum):
    PRODUCTION = "production"
    TEST = "test"


OnBlockCallback = Callable[[Action, list[str]], bool]


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
    ) -> None:
        self._mode = Mode(mode)
        self._auto_snapshot = auto_snapshot
        self._on_block = on_block
        self._rules = RuleEngine()
        self._budget = BudgetTracker(budget=budget)
        self._snapshots = SnapshotStore(max_snapshots=max_snapshots)
        self._history: list[ActionResult] = []
        self._tool_states: dict[str, Any] = {}
        self._tool_reversibility: dict[str, bool | None] = {}
        self._logger = ActionLogger(db_path, masker=None) if db_path else None
        self._rate_limiter = RateLimiter(
            global_rpm=global_rpm,
            global_rph=global_rph,
            tool_limits=tool_limits or {},
        )
        self._notifier = EventNotifier()
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
        self._estimator = CostEstimator()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "WarderBox":
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

    def _mask_args(self, args: dict) -> dict:
        if self._masker:
            return self._masker.mask(args)
        return args

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
                    args=kwargs if kwargs else {"args": list(args)},
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
        if self._capabilities:
            cap_ok, cap_reason = self._capabilities.check(
                getattr(self, '_agent_name', ''), action.tool_name,
                args=action.args, cost=cost,
            )
            if not cap_ok:
                result = ActionResult(
                    action=action, blocked=True,
                    block_reason=f"Capability denied: {cap_reason}",
                )
                self._history.append(result)
                if self._logger:
                    self._logger.log(result)
                self._notifier.emit("action_blocked", {
                    "tool": action.tool_name, "reason": result.block_reason,
                })
                return result

        if self._idempotency:
            dedup_status, cached = self._idempotency.check(
                action.tool_name, action.args,
            )
            if dedup_status == DedupeResult.DUPLICATE:
                result = ActionResult(
                    action=action, blocked=True,
                    block_reason=f"Duplicate call blocked (cached result available)",
                    output=cached,
                )
                self._history.append(result)
                if self._logger:
                    self._logger.log(result)
                self._notifier.emit("action_deduplicated", {
                    "tool": action.tool_name,
                })
                return result

        if self._circuit_breaker:
            cb_ok, cb_reason = self._circuit_breaker.should_allow(action.tool_name)
            if not cb_ok:
                if self._fallback and self._fallback.has_fallback(action.tool_name):
                    route = self._fallback.get_fallback(
                        action.tool_name, reason="circuit_breaker",
                    )
                    if route and route.fallback_fn:
                        self._fallback.record_fallback(action.tool_name)
                        try:
                            output = route.fallback_fn(*args, **kwargs)
                            result = ActionResult(
                                action=action, output=output, cost=route.fallback_cost,
                            )
                            self._budget.record(
                                action.id, route.fallback_tool, route.fallback_cost,
                            )
                            self._rate_limiter.record(action.tool_name)
                            self._history.append(result)
                            if self._logger:
                                self._logger.log(result)
                            self._notifier.emit("action_fallback", {
                                "tool": action.tool_name,
                                "fallback": route.fallback_tool,
                            })
                            return result
                        except Exception:
                            pass

                result = ActionResult(
                    action=action, blocked=True,
                    block_reason=f"Circuit breaker: {cb_reason}",
                )
                self._history.append(result)
                if self._logger:
                    self._logger.log(result)
                self._notifier.emit("action_blocked", {
                    "tool": action.tool_name, "reason": result.block_reason,
                })
                return result

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

        reservation = self._budget.reserve(action.id, action.tool_name, cost)
        if reservation is None:
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
            self._budget.release(reservation.reservation_id)
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

        if self._quota:
            q_exceeded, q_reason = self._quota.would_exceed(action.tool_name)
            if q_exceeded:
                self._budget.release(reservation.reservation_id)
                result = ActionResult(
                    action=action,
                    blocked=True,
                    block_reason=q_reason,
                )
                self._history.append(result)
                if self._logger:
                    self._logger.log(result)
                self._notifier.emit("quota_exceeded", {
                    "tool": action.tool_name, "reason": q_reason,
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
            self._budget.commit(reservation.reservation_id)
            self._rate_limiter.record(action.tool_name)
            self._history.append(result)
            if self._logger:
                self._logger.log(result)
            return result

        span = None
        if self._tracer:
            span = self._tracer.start_span(
                f"tool.{action.tool_name}",
                attributes={"tool.name": action.tool_name, "tool.cost": cost,
                            "tool.risk": action.risk.value},
            )

        t0 = time.time()

        if self._chaos and self._chaos.enabled:
            fault = self._chaos.should_inject(action.tool_name, action.args)
            if fault:
                try:
                    self._chaos.inject(fault, action.tool_name)
                except Exception as e:
                    result = ActionResult(
                        action=action, error=str(e),
                        snapshot_id=snapshot_id, cost=cost,
                    )
                    if self._circuit_breaker:
                        self._circuit_breaker.record_failure(action.tool_name)
                    self._notifier.emit("action_error", {
                        "tool": action.tool_name, "error": str(e),
                    })
                    self._budget.commit(reservation.reservation_id)
                    self._rate_limiter.record(action.tool_name)
                    self._history.append(result)
                    if self._logger:
                        self._logger.log(result)
                    if span:
                        span.set_attribute("error.message", str(e))
                        self._tracer.end_span(span, "error")
                    return result

        try:
            if self._timeout and not retry_policy:
                output = self._timeout.execute(
                    action.tool_name, func, args, kwargs,
                )
            elif retry_policy:
                output, _errors = execute_with_retry(func, args, kwargs, retry_policy)
            else:
                output = func(*args, **kwargs)

            if self._output_validator:
                check = self._output_validator.validate(action.tool_name, output)
                if not check.passed:
                    result = ActionResult(
                        action=action,
                        error=f"Output blocked: {'; '.join(check.violations)}",
                        snapshot_id=snapshot_id,
                        cost=cost,
                    )
                    self._budget.commit(reservation.reservation_id)
                    self._rate_limiter.record(action.tool_name)
                    if self._quota:
                        self._quota.record(action.tool_name)
                    self._history.append(result)
                    if self._logger:
                        self._logger.log(result)
                    self._notifier.emit("output_blocked", {
                        "tool": action.tool_name,
                        "violations": check.violations,
                    })
                    if span:
                        span.set_attribute("output.blocked", True)
                        self._tracer.end_span(span, "output_blocked")
                    return result
                output = check.filtered_output

            result = ActionResult(
                action=action,
                output=output,
                snapshot_id=snapshot_id,
                cost=cost,
                reversible=self._tool_reversibility.get(action.tool_name),
            )
        except ToolTimeoutError as e:
            result = ActionResult(
                action=action,
                error=str(e),
                snapshot_id=snapshot_id,
                cost=cost,
            )
            if self._circuit_breaker:
                self._circuit_breaker.record_failure(action.tool_name)
            self._notifier.emit("action_timeout", {
                "tool": action.tool_name, "error": str(e),
            })
        except Exception as e:
            result = ActionResult(
                action=action,
                error=str(e),
                snapshot_id=snapshot_id,
                cost=cost,
            )
            if self._circuit_breaker:
                self._circuit_breaker.record_failure(action.tool_name)
            self._notifier.emit("action_error", {
                "tool": action.tool_name, "error": str(e),
            })

        self._budget.commit(reservation.reservation_id)
        self._rate_limiter.record(action.tool_name)
        if self._quota:
            self._quota.record(action.tool_name)
        self._history.append(result)
        if self._logger:
            self._logger.log(result)

        if result.success:
            if self._circuit_breaker:
                self._circuit_breaker.record_success(action.tool_name)
            if self._idempotency and self._idempotency.is_protected(action.tool_name):
                self._idempotency.record(
                    action.tool_name, action.args, result.output,
                )
            self._notifier.emit("action_completed", {
                "tool": action.tool_name, "cost": cost,
            })

        if self._budget.warning:
            self._notifier.emit("budget_warning", {
                "spent": self._budget.spent, "budget": self._budget.budget,
            })

        duration = time.time() - t0
        self._estimator.record(
            action.tool_name,
            cost=result.cost,
            duration=duration,
            success=result.success,
        )

        if span:
            span.set_attribute("tool.duration_ms", duration * 1000)
            span.set_attribute("tool.blocked", result.blocked)
            if result.error:
                span.set_attribute("error.message", result.error)
            self._tracer.end_span(span, "error" if result.error else "ok")

        return result

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
        results = []
        for r in self._history:
            results.append({
                "id": r.action.id,
                "tool": r.action.tool_name,
                "risk": r.action.risk.value,
                "blocked": r.blocked,
                "block_reason": r.block_reason,
                "cost": r.cost,
                "success": r.success,
                "has_snapshot": r.snapshot_id is not None,
                "reversible": r.reversible,
                "timestamp": r.action.timestamp,
            })
        return results

    def last_result(self) -> ActionResult | None:
        return self._history[-1] if self._history else None

    # -- Stats --

    def stats(self) -> dict:
        total = len(self._history)
        blocked = sum(1 for r in self._history if r.blocked)
        errors = sum(1 for r in self._history if r.error)
        return {
            "total_actions": total,
            "blocked": blocked,
            "errors": errors,
            "successful": total - blocked - errors,
            "snapshots": self._snapshots.count,
            "budget": self._budget.summary(),
            "rate_limits": self._rate_limiter.stats(),
        }
