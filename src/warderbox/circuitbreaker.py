"""Circuit breaker pattern for AI agent tools.

Automatically disables tools that fail repeatedly, preventing cascading
failures and wasted budget. Inspired by Netflix Hystrix / resilience4j.

States:
    CLOSED   — normal operation, failures are counted
    OPEN     — tool is disabled, calls are blocked immediately
    HALF_OPEN — after reset_timeout, one probe call is allowed through

Usage:
    cb = CircuitBreaker(failure_threshold=5, reset_timeout=60.0)
    cb.record_success("search_api")
    cb.record_failure("search_api")
    cb.is_open("search_api")  # True after 5 failures
    # After 60s, state becomes HALF_OPEN and one call is allowed
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ToolCircuit:
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    state: CircuitState = CircuitState.CLOSED
    half_open_admitted: bool = False


class CircuitBreaker:
    """Per-tool circuit breaker with configurable thresholds."""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
        success_threshold: int = 2,
        on_open: callable | None = None,
        on_close: callable | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._success_threshold = success_threshold
        self._on_open = on_open
        self._on_close = on_close
        self._circuits: dict[str, ToolCircuit] = {}

    def _get(self, tool_name: str) -> ToolCircuit:
        if tool_name not in self._circuits:
            self._circuits[tool_name] = ToolCircuit()
        return self._circuits[tool_name]

    def _check_half_open(self, circuit: ToolCircuit) -> None:
        if circuit.state != CircuitState.OPEN:
            return
        elapsed = time.time() - circuit.last_failure_time
        if elapsed >= self._reset_timeout:
            circuit.state = CircuitState.HALF_OPEN
            circuit.half_open_admitted = False

    def is_open(self, tool_name: str) -> bool:
        circuit = self._get(tool_name)
        self._check_half_open(circuit)
        return circuit.state == CircuitState.OPEN

    def should_allow(self, tool_name: str) -> tuple[bool, str | None]:
        circuit = self._get(tool_name)
        self._check_half_open(circuit)

        if circuit.state == CircuitState.CLOSED:
            return True, None

        if circuit.state == CircuitState.HALF_OPEN:
            if not circuit.half_open_admitted:
                circuit.half_open_admitted = True
                return True, None
            return False, f"Circuit half-open for '{tool_name}', probe in progress"

        return False, (
            f"Circuit open for '{tool_name}': "
            f"{circuit.failure_count} consecutive failures"
        )

    def record_success(self, tool_name: str) -> None:
        circuit = self._get(tool_name)

        if circuit.state == CircuitState.HALF_OPEN:
            circuit.success_count += 1
            if circuit.success_count >= self._success_threshold:
                circuit.state = CircuitState.CLOSED
                circuit.failure_count = 0
                circuit.success_count = 0
                if self._on_close:
                    self._on_close(tool_name)
        else:
            circuit.failure_count = 0
            circuit.success_count = 0

    def record_failure(self, tool_name: str) -> None:
        circuit = self._get(tool_name)

        if circuit.state == CircuitState.HALF_OPEN:
            circuit.state = CircuitState.OPEN
            circuit.last_failure_time = time.time()
            circuit.success_count = 0
            if self._on_open:
                self._on_open(tool_name)
            return

        circuit.failure_count += 1
        circuit.last_failure_time = time.time()

        if circuit.failure_count >= self._failure_threshold:
            circuit.state = CircuitState.OPEN
            if self._on_open:
                self._on_open(tool_name)

    def get_state(self, tool_name: str) -> CircuitState:
        circuit = self._get(tool_name)
        self._check_half_open(circuit)
        return circuit.state

    def reset(self, tool_name: str) -> None:
        if tool_name in self._circuits:
            self._circuits[tool_name] = ToolCircuit()

    def reset_all(self) -> None:
        self._circuits.clear()

    def stats(self) -> dict[str, dict]:
        result = {}
        for name, circuit in self._circuits.items():
            self._check_half_open(circuit)
            result[name] = {
                "state": circuit.state.value,
                "failure_count": circuit.failure_count,
                "success_count": circuit.success_count,
            }
        return result
