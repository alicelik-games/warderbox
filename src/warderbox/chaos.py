"""Chaos engineering / fault injection for AI agent resilience testing.

Inject controlled failures into agent tool calls to verify that the agent
handles errors gracefully — circuit breakers trip, retries fire, budgets
hold, and no data is lost.

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

    def __init__(self, enabled: bool = True, seed: int | None = None) -> None:
        self._enabled = enabled
        self._rules: list[FaultRule] = []
        self._history: list[FaultEvent] = []
        self._rng = random.Random(seed)

    @property
    def enabled(self) -> bool:
        return self._enabled

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

    def inject(self, rule: FaultRule, tool_name: str) -> None:
        rule._injection_count += 1

        event = FaultEvent(
            tool_name=tool_name,
            fault_type=rule.fault_type.value,
        )

        if rule.fault_type == FaultType.ERROR:
            event.details = rule.error_message
            self._history.append(event)
            raise RuntimeError(f"[CHAOS] {rule.error_message}")

        if rule.fault_type == FaultType.LATENCY:
            delay = rule.latency_ms / 1000.0
            event.details = f"Injected {rule.latency_ms}ms latency"
            self._history.append(event)
            time.sleep(delay)
            return

        if rule.fault_type == FaultType.TIMEOUT:
            event.details = f"Simulated timeout after {rule.latency_ms}ms"
            self._history.append(event)
            time.sleep(rule.latency_ms / 1000.0)
            raise TimeoutError(f"[CHAOS] Simulated timeout for {tool_name}")

        if rule.fault_type == FaultType.CORRUPT_OUTPUT:
            event.details = "Output will be corrupted"
            self._history.append(event)
            return

    def maybe_corrupt_output(self, tool_name: str, output):
        if not self._enabled:
            return output

        for rule in self._rules:
            if rule.tool_name != tool_name and rule.tool_name != "*":
                continue
            if rule.fault_type == FaultType.CORRUPT_OUTPUT and not rule.exhausted:
                if isinstance(output, str):
                    return f"[CORRUPTED] {output[::-1]}"
                if isinstance(output, dict):
                    return {"__corrupted__": True, "original_keys": list(output.keys())}
                return None

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
