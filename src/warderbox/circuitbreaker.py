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

import threading
import time
from dataclasses import dataclass
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
        # Reentrant so on_open/on_close callbacks may call back into the
        # breaker (e.g. read get_state) without deadlocking.
        self._lock = threading.RLock()

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
        with self._lock:
            circuit = self._get(tool_name)
            self._check_half_open(circuit)
            return circuit.state == CircuitState.OPEN

    def should_allow(self, tool_name: str) -> tuple[bool, str | None]:
        with self._lock:
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
        fire_close = False
        with self._lock:
            circuit = self._get(tool_name)

            if circuit.state == CircuitState.HALF_OPEN:
                circuit.success_count += 1
                if circuit.success_count >= self._success_threshold:
                    circuit.state = CircuitState.CLOSED
                    circuit.failure_count = 0
                    circuit.success_count = 0
                    circuit.half_open_admitted = False
                    fire_close = True
                else:
                    # Probe succeeded but more are needed: admit the next one
                    # instead of locking the circuit in HALF_OPEN forever.
                    circuit.half_open_admitted = False
            else:
                circuit.failure_count = 0
                circuit.success_count = 0
        # Callbacks run OUTSIDE the lock so a slow/other-thread callback does
        # not stall concurrent breaker calls, and in try/except so a faulty
        # callback never corrupts the result of the call that triggered it.
        if fire_close and self._on_close:
            self._safe_callback(self._on_close, tool_name)

    def record_failure(self, tool_name: str) -> None:
        fire_open = False
        with self._lock:
            circuit = self._get(tool_name)

            if circuit.state == CircuitState.HALF_OPEN:
                circuit.state = CircuitState.OPEN
                circuit.last_failure_time = time.time()
                circuit.success_count = 0
                circuit.half_open_admitted = False
                fire_open = True
            else:
                circuit.failure_count += 1
                circuit.last_failure_time = time.time()
                if circuit.failure_count >= self._failure_threshold:
                    circuit.state = CircuitState.OPEN
                    fire_open = True

        if fire_open and self._on_open:
            self._safe_callback(self._on_open, tool_name)

    @staticmethod
    def _safe_callback(cb, tool_name: str) -> None:
        try:
            cb(tool_name)
        except Exception:
            pass

    def get_state(self, tool_name: str) -> CircuitState:
        with self._lock:
            circuit = self._get(tool_name)
            self._check_half_open(circuit)
            return circuit.state

    def reset(self, tool_name: str) -> None:
        with self._lock:
            if tool_name in self._circuits:
                self._circuits[tool_name] = ToolCircuit()

    def reset_all(self) -> None:
        with self._lock:
            self._circuits.clear()

    def stats(self) -> dict[str, dict]:
        with self._lock:
            result = {}
            for name, circuit in self._circuits.items():
                self._check_half_open(circuit)
                result[name] = {
                    "state": circuit.state.value,
                    "failure_count": circuit.failure_count,
                    "success_count": circuit.success_count,
                }
            return result
