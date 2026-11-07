"""Chaos engineering / fault injection for AI agent resilience testing.

Inject controlled failures into agent tool calls to verify that the agent
handles errors gracefully — circuit breakers trip, retries fire, budgets
hold, and no data is lost.

Faults fire only in test mode by default. Production injection requires an
explicit ``ChaosEngine(allow_production=True)`` and raises a RuntimeWarning when
attached; that option is for deliberate resilience drills, not normal use.

Usage:
    chaos = ChaosEngine()
    shield.set_chaos(chaos)

    # Random failures (20% of calls)
    chaos.add_fault("search_api", FaultType.ERROR, probability=0.2)

    # Fixed latency injection
    chaos.add_fault("slow_api", FaultType.LATENCY, latency_ms=2000)

    # Kill switch — always fail
    chaos.add_fault("broken_tool", FaultType.ERROR, probability=1.0)

    # Conditional faults
    chaos.add_fault("db_query", FaultType.ERROR,
                    condition=lambda args: "prod" in str(args))
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum


class FaultType(Enum):
    ERROR = "error"
    LATENCY = "latency"
    CORRUPT_OUTPUT = "corrupt_output"
    TIMEOUT = "timeout"


@dataclass
class FaultRule:
    tool_name: str
    fault_type: FaultType
    probability: float = 1.0
    error_message: str = "Chaos fault injected"
    latency_ms: float = 1000.0
    condition: callable | None = None
    max_injections: int | None = None
    _injection_count: int = 0

    @property
    def exhausted(self) -> bool:
        if self.max_injections is None:
            return False
        return self._injection_count >= self.max_injections


@dataclass
class FaultEvent:
    tool_name: str
    fault_type: str
    timestamp: float = field(default_factory=time.time)
    details: str = ""


class ChaosEngine:
    """Injects controlled faults into agent tool execution."""

    def __init__(
        self,
        enabled: bool = True,
        seed: int | None = None,
        allow_production: bool = False,
    ) -> None:
        self._enabled = enabled
        self._rules: list[FaultRule] = []
        self._history: list[FaultEvent] = []
        self._rng = random.Random(seed)
        self._allow_production = allow_production

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def allow_production(self) -> bool:
        return self._allow_production

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def add_fault(
        self,
        tool_name: str,
        fault_type: FaultType,
        probability: float = 1.0,
        error_message: str = "Chaos fault injected",
        latency_ms: float = 1000.0,
        condition: callable | None = None,
        max_injections: int | None = None,
    ) -> None:
        self._rules.append(FaultRule(
            tool_name=tool_name,
            fault_type=fault_type,
            probability=min(max(probability, 0.0), 1.0),
            error_message=error_message,
            latency_ms=latency_ms,
            condition=condition,
            max_injections=max_injections,
        ))

    def remove_faults(self, tool_name: str) -> int:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.tool_name != tool_name]
        return before - len(self._rules)

    def clear_all(self) -> None:
        self._rules.clear()

    def should_inject(self, tool_name: str, args: dict | None = None) -> FaultRule | None:
        if not self._enabled:
            return None

        for rule in self._rules:
            if rule.tool_name != tool_name and rule.tool_name != "*":
                continue
            if rule.exhausted:
                continue
            if rule.condition and not rule.condition(args or {}):
                continue
            if self._rng.random() > rule.probability:
                continue
            return rule

        return None

    def _record(self, rule: FaultRule, tool_name: str, details: str) -> None:
        rule._injection_count += 1
        self._history.append(FaultEvent(
            tool_name=tool_name,
            fault_type=rule.fault_type.value,
            details=details,
        ))

    def _raise_for(self, rule: FaultRule, tool_name: str) -> None:
        if rule.fault_type == FaultType.ERROR:
            raise RuntimeError(f"[CHAOS] {rule.error_message}")
        if rule.fault_type == FaultType.TIMEOUT:
            # Use WarderBox's TimeoutError so the pipeline treats this as a
            # timeout (fires action_timeout), not a generic error.
            from warderbox.timeout import TimeoutError as ToolTimeoutError
            raise ToolTimeoutError(f"[CHAOS] Simulated timeout for {tool_name}")

    @staticmethod
    def _describe(rule: FaultRule) -> str:
        if rule.fault_type == FaultType.ERROR:
            return rule.error_message
        if rule.fault_type == FaultType.LATENCY:
            return f"Injected {rule.latency_ms}ms latency"
        if rule.fault_type == FaultType.TIMEOUT:
            return f"Simulated timeout after {rule.latency_ms}ms"
        return "Output will be corrupted"

    @staticmethod
    def _delay(rule: FaultRule) -> float:
        if rule.fault_type in (FaultType.LATENCY, FaultType.TIMEOUT):
            return rule.latency_ms / 1000.0
        return 0.0

    def inject(self, rule: FaultRule, tool_name: str) -> None:
        """Apply a fault. Raises for ERROR/TIMEOUT, sleeps for LATENCY.

        CORRUPT_OUTPUT does nothing here: the pipeline passes the tool's result
        through :meth:`corrupt` after the tool runs.
        """
        self._record(rule, tool_name, self._describe(rule))
        delay = self._delay(rule)
        if delay:
            time.sleep(delay)
        self._raise_for(rule, tool_name)

    async def ainject(self, rule: FaultRule, tool_name: str) -> None:
        """Async :meth:`inject`: latency must not freeze the event loop."""
        self._record(rule, tool_name, self._describe(rule))
        delay = self._delay(rule)
        if delay:
            await asyncio.sleep(delay)
        self._raise_for(rule, tool_name)

    @staticmethod
    def corrupt(output):
        """Return a corrupted copy of a tool's output."""
        if isinstance(output, str):
            return f"[CORRUPTED] {output[::-1]}"
        if isinstance(output, dict):
            return {"__corrupted__": True, "original_keys": list(output.keys())}
        return None

    def maybe_corrupt_output(self, tool_name: str, output):
        if not self._enabled:
            return output

        for rule in self._rules:
            if rule.tool_name != tool_name and rule.tool_name != "*":
                continue
            if rule.fault_type == FaultType.CORRUPT_OUTPUT and not rule.exhausted:
                return self.corrupt(output)

        return output

    @property
    def history(self) -> list[FaultEvent]:
        return list(self._history)

    def stats(self) -> dict:
        total = len(self._history)
        by_type: dict[str, int] = {}
        by_tool: dict[str, int] = {}
        for e in self._history:
            by_type[e.fault_type] = by_type.get(e.fault_type, 0) + 1
            by_tool[e.tool_name] = by_tool.get(e.tool_name, 0) + 1
        return {
            "total_injections": total,
            "by_type": by_type,
            "by_tool": by_tool,
            "active_rules": len([r for r in self._rules if not r.exhausted]),
        }
