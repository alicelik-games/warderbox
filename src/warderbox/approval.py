"""Human-in-the-loop approval workflows for AI agent actions.

Provides configurable approval gates that pause execution until a human
approves or denies the action. Supports timeout with auto-deny, callback-
based approval, and escalation chains.

Usage:
    approver = ApprovalManager(
        default_timeout=30.0,
        on_approval_request=my_callback,
    )

    # Register tool-specific policies
    approver.require_approval("send_email", timeout=60.0)
    approver.require_approval("delete_record", auto_deny=True)

    # Check in shield flow
    decision = approver.request_approval(action)
    if decision.approved:
        execute(action)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class ApprovalDecision(Enum):
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"
    ESCALATED = "escalated"
    NOT_REQUIRED = "not_required"


@dataclass
class ApprovalResult:
    decision: ApprovalDecision
    tool_name: str
    approver: str | None = None
    reason: str | None = None
    elapsed: float = 0.0

    @property
    def approved(self) -> bool:
        return self.decision in (
            ApprovalDecision.APPROVED,
            ApprovalDecision.NOT_REQUIRED,
        )


@dataclass
class ApprovalPolicy:
    timeout: float = 30.0
    auto_deny_on_timeout: bool = True
    escalate_to: callable | None = None
    required_for_risk: str | None = None


class ApprovalManager:
    """Manages human-in-the-loop approval for agent actions."""

    def __init__(
        self,
        default_timeout: float = 30.0,
        on_approval_request: callable | None = None,
        auto_approve_low_risk: bool = True,
    ) -> None:
        self._default_timeout = default_timeout
        self._on_request = on_approval_request
        self._auto_approve_low = auto_approve_low_risk
        self._policies: dict[str, ApprovalPolicy] = {}
        self._pending: dict[str, threading.Event] = {}
        self._decisions: dict[str, ApprovalResult] = {}
        self._history: list[ApprovalResult] = []
        self._lock = threading.Lock()

    def require_approval(
        self,
        tool_name: str,
        timeout: float | None = None,
        auto_deny: bool = True,
        escalate_to: callable | None = None,
    ) -> None:
        self._policies[tool_name] = ApprovalPolicy(
            timeout=timeout or self._default_timeout,
            auto_deny_on_timeout=auto_deny,
            escalate_to=escalate_to,
        )

    def require_approval_for_risk(
        self,
        risk_level: str,
        timeout: float | None = None,
        auto_deny: bool = True,
    ) -> None:
        policy = ApprovalPolicy(
            timeout=timeout or self._default_timeout,
            auto_deny_on_timeout=auto_deny,
            required_for_risk=risk_level.upper(),
        )
        self._policies[f"__risk__{risk_level.upper()}"] = policy

    def needs_approval(self, tool_name: str, risk_level: str = "LOW") -> bool:
        if tool_name in self._policies:
            return True
        risk_key = f"__risk__{risk_level.upper()}"
        if risk_key in self._policies:
            return True
        return False

    def request_approval(
        self,
        tool_name: str,
        args: dict | None = None,
        risk_level: str = "LOW",
        action_id: str | None = None,
    ) -> ApprovalResult:
        if not self.needs_approval(tool_name, risk_level):
            result = ApprovalResult(
                decision=ApprovalDecision.NOT_REQUIRED,
                tool_name=tool_name,
            )
            return result

        policy = self._policies.get(tool_name)
        if not policy:
            risk_key = f"__risk__{risk_level.upper()}"
            policy = self._policies.get(risk_key, ApprovalPolicy())

        request_id = action_id or f"{tool_name}_{time.time()}"

        event = threading.Event()
        with self._lock:
            self._pending[request_id] = event

        if self._on_request:
            self._on_request(
                request_id=request_id,
                tool_name=tool_name,
                args=args or {},
                risk_level=risk_level,
            )

        start = time.time()
        approved = event.wait(timeout=policy.timeout)
        elapsed = time.time() - start

        with self._lock:
            self._pending.pop(request_id, None)

            if request_id in self._decisions:
                result = self._decisions.pop(request_id)
                result.elapsed = elapsed
            elif approved:
                result = ApprovalResult(
                    decision=ApprovalDecision.APPROVED,
                    tool_name=tool_name,
                    elapsed=elapsed,
                )
            else:
                if policy.escalate_to:
                    try:
                        escalation = policy.escalate_to(
                            tool_name=tool_name,
                            args=args,
                            risk_level=risk_level,
                        )
                        if escalation:
                            result = ApprovalResult(
                                decision=ApprovalDecision.APPROVED,
                                tool_name=tool_name,
                                approver="escalation",
                                elapsed=elapsed,
                            )
                        else:
                            result = ApprovalResult(
                                decision=ApprovalDecision.ESCALATED,
                                tool_name=tool_name,
                                reason="Escalation denied",
                                elapsed=elapsed,
                            )
                    except Exception:
                        result = ApprovalResult(
                            decision=ApprovalDecision.DENIED,
                            tool_name=tool_name,
                            reason="Escalation failed",
                            elapsed=elapsed,
                        )
                elif policy.auto_deny_on_timeout:
                    result = ApprovalResult(
                        decision=ApprovalDecision.TIMEOUT,
                        tool_name=tool_name,
                        reason=f"Approval timed out after {policy.timeout}s",
                        elapsed=elapsed,
                    )
                else:
                    result = ApprovalResult(
                        decision=ApprovalDecision.DENIED,
                        tool_name=tool_name,
                        reason="No approval received",
                        elapsed=elapsed,
                    )

        self._history.append(result)
        return result

    def approve(
        self,
        request_id: str,
        approver: str | None = None,
        reason: str | None = None,
    ) -> bool:
        with self._lock:
            event = self._pending.get(request_id)
            if not event:
                return False
            self._decisions[request_id] = ApprovalResult(
                decision=ApprovalDecision.APPROVED,
                tool_name="",
                approver=approver,
                reason=reason,
            )
            event.set()
            return True

    def deny(
        self,
        request_id: str,
        approver: str | None = None,
        reason: str | None = None,
    ) -> bool:
        with self._lock:
            event = self._pending.get(request_id)
            if not event:
                return False
            self._decisions[request_id] = ApprovalResult(
                decision=ApprovalDecision.DENIED,
                tool_name="",
                approver=approver,
                reason=reason,
            )
            event.set()
            return True

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def history(self) -> list[ApprovalResult]:
        return list(self._history)

    def stats(self) -> dict:
        total = len(self._history)
        approved = sum(1 for r in self._history if r.approved)
        denied = sum(
            1 for r in self._history
            if r.decision == ApprovalDecision.DENIED
        )
        timed_out = sum(
            1 for r in self._history
            if r.decision == ApprovalDecision.TIMEOUT
        )
        return {
            "total_requests": total,
            "approved": approved,
            "denied": denied,
            "timed_out": timed_out,
            "pending": self.pending_count,
        }
