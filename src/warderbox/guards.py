"""The guard pipeline every tool call passes through.

A primary call and a fallback call run the same guards in the same order, so a
gate cannot exist on one path and be missing on the other. Each guard may stop
the call, and each guard that held something is told to let go, in reverse
order, if a later guard stops the call or the call is interrupted.

Add your own with ``shield.add_guard()``:

    class BusinessHours(Guard):
        name = "business_hours"

        def check(self, ctx):
            if not is_business_hours():
                return Block(f"{ctx.tool} is only allowed in business hours")

    shield.add_guard(BusinessHours())
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from warderbox.action import Action, ActionResult, RiskLevel
from warderbox.idempotency import DedupeResult
from warderbox.state import StateBackendError

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass
class Block:
    """Stop the call. ``event`` is the notifier event to emit, if any."""

    reason: str
    event: str | None = "action_blocked"


@dataclass
class CallContext:
    """Everything known about one call as it moves through the pipeline."""

    shield: Any
    action: Action
    tool: str  # the tool that will really run (the fallback, on a fallback)
    cost: float
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    risk_override: RiskLevel | None = None
    route: Any = None  # set when this call is a fallback
    reasons: list[str] = field(default_factory=list)
    reservation: Any = None
    snapshot_id: str | None = None
    protected: bool = False  # holds an idempotency slot
    started: bool = False  # the tool body may have run
    charge: bool = True  # False: the failed call costs nothing
    settled: bool = False  # guards have committed; nothing left to release
    span: Any = None
    span_status: str | None = None
    t0: float = 0.0
    entered: list[Guard] = field(default_factory=list)
    inflight: Any = None  # a retry resumes a timed-out attempt through this

    @property
    def is_fallback(self) -> bool:
        return self.route is not None

    @property
    def label(self) -> str:
        return " (fallback)" if self.is_fallback else ""


class Guard:
    """One gate in the pipeline. Override what you need.

    ``check`` and ``reserve`` return None to continue, or a :class:`Block`.
    ``release`` undoes ``reserve`` and only runs if ``reserve`` returned None.
    ``commit`` runs after the tool finishes. An exception raised by ``check``
    or ``reserve`` stops the call rather than letting it through: the tool does
    not run and the caller gets a blocked result.
    """

    name = "guard"
    applies_to_fallback = True
    failure_label = "Guard failed"

    def check(self, ctx: CallContext) -> Block | ActionResult | None:
        return None

    def reserve(self, ctx: CallContext) -> Block | ActionResult | None:
        return None

    def release(self, ctx: CallContext) -> None:
        pass

    def commit(self, ctx: CallContext, result: ActionResult) -> None:
        pass


def _backend_down(error: Exception) -> Block:
    return Block(f"State backend unavailable: {error}", "state_backend_error")


class CapabilityGuard(Guard):
    name = "capabilities"

    def check(self, ctx):
        caps = ctx.shield._capabilities
        if not caps:
            return None
        # A fallback must be permitted in its own right: being allowed to call
        # the primary does not grant it.
        allowed, reason = caps.check(
            ctx.shield._agent_name, ctx.tool, args=ctx.action.args, cost=ctx.cost,
        )
        if not allowed:
            return Block(f"Capability denied{ctx.label}: {reason}")
        return None


class IdempotencyCheckGuard(Guard):
    name = "idempotency_check"
    failure_label = "Idempotency backend failed"
    applies_to_fallback = False

    def check(self, ctx):
        guard = ctx.shield._idempotency
        if not guard:
            return None
        status, cached = guard.check(ctx.tool, ctx.action.args)
        if status == DedupeResult.DUPLICATE:
            return ctx.shield._deduplicated(ctx, cached)
        return None


class CircuitBreakerGuard(Guard):
    name = "circuit_breaker"
    applies_to_fallback = False

    def check(self, ctx):
        shield = ctx.shield
        breaker = shield._circuit_breaker
        if not breaker:
            return None
        allowed, reason = breaker.should_allow(ctx.tool)
        if allowed:
            return None
        if shield._fallback and shield._fallback.has_fallback(ctx.tool):
            route = shield._fallback.get_fallback(ctx.tool, reason="circuit_breaker")
            if route and route.fallback_fn:
                return shield._run_fallback(ctx, route)
        return Block(f"Circuit breaker: {reason}")


class RiskGuard(Guard):
    name = "risk"

    def check(self, ctx):
        shield = ctx.shield
        risk, reasons = shield._rules.evaluate(ctx.action)
        if ctx.is_fallback:
            # Rules that key on the tool name must see the tool that will run.
            other_risk, other_reasons = shield._rules.evaluate(
                replace(ctx.action, tool_name=ctx.tool)
            )
            if _RISK_ORDER[other_risk] > _RISK_ORDER[risk]:
                risk = other_risk
            reasons = reasons + [r for r in other_reasons if r not in reasons]
        # The per-tool `risk=` is a floor, never a ceiling: declaring a tool LOW
        # can never suppress a CRITICAL pattern detected in its arguments.
        if ctx.risk_override is not None:
            if _RISK_ORDER[ctx.risk_override] > _RISK_ORDER[risk]:
                risk = ctx.risk_override
            if not reasons:
                reasons = [f"declared risk {ctx.risk_override.value}"]
        ctx.action.risk = risk
        ctx.reasons = reasons
        if risk == RiskLevel.CRITICAL:
            return Block(f"CRITICAL risk blocked{ctx.label}: {'; '.join(reasons)}")
        if risk == RiskLevel.HIGH and shield._on_block:
            if not shield._consult_on_block(ctx.action, reasons):
                return Block(f"Blocked by user{ctx.label}: {'; '.join(reasons)}")
        return None


class ApprovalGuard(Guard):
    name = "approval"

    def check(self, ctx):
        approval = ctx.shield._approval
        if not approval:
            return None
        risk = ctx.action.risk.value
        # Ask for whichever tool carries the policy, primary first, so a policy
        # on the fallback is not bypassed by asking about an unpoliced primary.
        tools = [ctx.action.tool_name]
        if ctx.tool != ctx.action.tool_name:
            tools.append(ctx.tool)
        target = next((t for t in tools if approval.needs_approval(t, risk)), None)
        if target is None:
            return None
        decision = approval.request_approval(
            target, args=ctx.action.args, risk_level=risk, action_id=ctx.action.id,
        )
        if decision.approved:
            return None
        return Block(
            f"Approval {decision.decision.value}{ctx.label}: "
            f"{decision.reason or 'not approved'}",
            "approval_denied",
        )


class BudgetGuard(Guard):
    name = "budget"

    def reserve(self, ctx):
        budget = ctx.shield._budget
        try:
            reservation = budget.reserve(ctx.action.id, ctx.tool, ctx.cost)
            if reservation is not None:
                ctx.reservation = reservation
                return None
            spent = budget.spent
        except StateBackendError as e:
            return _backend_down(e)
        ctx.shield._notifier.emit("budget_exceeded", {
            "tool": ctx.tool, "spent": spent, "budget": budget.budget,
        })
        prefix = "Fallback budget" if ctx.is_fallback else "Budget"
        return Block(
            f"{prefix} exceeded (spent: ${spent:.2f}, "
            f"budget: ${budget.budget:.2f}, action cost: ${ctx.cost:.2f})",
            None,
        )

    def release(self, ctx):
        ctx.shield._budget.release(ctx.reservation.reservation_id)

    def commit(self, ctx, result):
        budget = ctx.shield._budget
        reservation_id = ctx.reservation.reservation_id
        if not ctx.charge:
            budget.release(reservation_id)
            return
        try:
            budget.commit(reservation_id, result.actual_cost)
        except Exception:
            budget.release(reservation_id)


class RateLimitGuard(Guard):
    name = "rate_limit"
    failure_label = "Rate limiter failed"

    def reserve(self, ctx):
        try:
            allowed, reason = ctx.shield._rate_limiter.reserve(ctx.tool)
        except StateBackendError as e:
            return _backend_down(e)
        return None if allowed else Block(reason, "rate_limited")

    def release(self, ctx):
        try:
            ctx.shield._rate_limiter.release(ctx.tool)
        except StateBackendError as e:
            ctx.shield._backend_error(e)


class QuotaGuard(Guard):
    name = "quota"
    failure_label = "Quota reservation failed"

    def reserve(self, ctx):
        quota = ctx.shield._quota
        if not quota:
            return None
        try:
            allowed, reason = quota.reserve(ctx.tool)
        except StateBackendError as e:
            return _backend_down(e)
        return None if allowed else Block(reason, "quota_exceeded")

    def release(self, ctx):
        quota = ctx.shield._quota
        if not quota:
            return
        try:
            quota.release(ctx.tool)
        except StateBackendError as e:
            ctx.shield._backend_error(e)


class IdempotencyGuard(Guard):
    """Atomically claim the slot right before running, so concurrent calls and
    caller retries cannot fire a side effect twice."""

    name = "idempotency"
    failure_label = "Idempotency backend failed"

    def reserve(self, ctx):
        shield = ctx.shield
        guard = shield._idempotency
        # A dry run has no side effect to protect and must leave no marker.
        if not guard or shield.is_test_mode or not guard.is_protected(ctx.tool):
            return None
        status, cached = guard.begin(ctx.tool, ctx.action.args)
        if status == DedupeResult.DUPLICATE:
            return shield._deduplicated(ctx, cached)
        ctx.protected = True
        return None

    def release(self, ctx):
        if not ctx.protected:
            return
        try:
            # Keep the marker if the body may have run: the side effect may
            # already have landed, so a retry must stay blocked.
            ctx.shield._idempotency.fail(
                ctx.tool, ctx.action.args, keep=ctx.started
            )
        except Exception:
            pass

    def commit(self, ctx, result):
        if not ctx.protected:
            return
        guard = ctx.shield._idempotency
        try:
            if result.success:
                guard.complete(ctx.tool, ctx.action.args, result.output)
            else:
                guard.fail(ctx.tool, ctx.action.args)
        except Exception as e:
            # A backend failure must not turn a finished call into an exception.
            ctx.shield._notifier.emit("idempotency_error", {
                "tool": ctx.tool, "error": str(e),
            })


class SnapshotGuard(Guard):
    name = "snapshot"
    failure_label = "Snapshot failed"

    def reserve(self, ctx):
        shield = ctx.shield
        if shield._auto_snapshot and ctx.tool in shield._tool_states:
            state = shield._tool_states[ctx.tool]["capture"]()
            ctx.snapshot_id = shield._snapshots.capture(
                ctx.action.id, ctx.tool, state
            ).id


class TestModeGuard(Guard):
    """Last gate: a dry run stops here, after every limit has been exercised."""

    __test__ = False  # not a pytest test class
    name = "test_mode"

    def check(self, ctx):
        return ctx.shield._dry_run(ctx) if ctx.shield.is_test_mode else None


def default_guards() -> list[Guard]:
    return [
        CapabilityGuard(),
        IdempotencyCheckGuard(),
        CircuitBreakerGuard(),
        RiskGuard(),
        ApprovalGuard(),
        BudgetGuard(),
        RateLimitGuard(),
        QuotaGuard(),
        IdempotencyGuard(),
        SnapshotGuard(),
        TestModeGuard(),
    ]
