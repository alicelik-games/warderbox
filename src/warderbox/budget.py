"""Budget tracker for agent cost control.

Supports thread-safe cost reservation and post-execution reconciliation
with actual provider charges.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from warderbox.state import StateBackend, StateBackendError


@dataclass
class CostEntry:
    """A single cost record."""

    action_id: str
    tool_name: str
    cost: float
    timestamp: float
    estimated_cost: float | None = None
    actual_cost: float | None = None
    reconciled: bool = False


@dataclass
class Reservation:
    """A pre-execution cost reservation."""

    reservation_id: str
    action_id: str
    tool_name: str
    estimated_cost: float
    timestamp: float = field(default_factory=time.time)


class BudgetTracker:
    """Tracks cumulative cost and enforces a budget limit.

    Thread-safe: uses a lock around reserve/commit/release so concurrent
    workers cannot overshoot the budget. With a ``backend`` the totals live in
    shared state, so the limit holds across every worker using the same
    namespace. Cost entries stay local to each process.
    """

    def __init__(
        self, budget: float | None = None, warn_at: float = 0.8,
        max_entries: int = 100_000,
        backend: StateBackend | None = None,
        namespace: str = "default",
        reservation_ttl: float = 3600.0,
    ) -> None:
        self._backend = backend
        self._namespace = namespace
        self._ttl = reservation_ttl
        # A charge that could not reach the backend. It is retried before the
        # next reservation so real spend is never silently forgotten.
        self._pending: list[tuple[str | None, float]] = []
        self.on_backend_error: Callable[[Exception], None] | None = None
        self._budget = budget
        self._warn_at = warn_at
        # Bounded history of cost entries; `spent` is tracked separately so
        # trimming old entries never changes the cumulative total.
        self._entries: deque[CostEntry] = deque(maxlen=max_entries)
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.Lock()
        self._total_reserved: float = 0.0
        self._reconciliation_drift: float = 0.0
        self._spent: float = 0.0  # running total, kept O(1)

    @property
    def budget(self) -> float | None:
        return self._budget

    @property
    def shared(self) -> bool:
        return self._backend is not None

    def _report_backend_error(self, error: Exception) -> None:
        if self.on_backend_error is not None:
            try:
                self.on_backend_error(error)
            except Exception:
                pass

    def _backend_charge(self, reservation_id: str | None, cost: float) -> None:
        try:
            if reservation_id is None:
                self._backend.budget_adjust(self._namespace, cost)
            else:
                self._backend.budget_commit(self._namespace, reservation_id, cost)
        except StateBackendError as e:
            with self._lock:
                self._pending.append((reservation_id, cost))
            self._report_backend_error(e)

    def _flush_pending(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for reservation_id, cost in pending:
            self._backend_charge(reservation_id, cost)

    def _shared_totals(self) -> tuple[float, float, int]:
        spent, held, count = self._backend.budget_totals(self._namespace)
        with self._lock:
            unsent = sum(cost for _id, cost in self._pending)
        return spent + unsent, held, count

    @property
    def spent(self) -> float:
        # Round to sub-nanocent precision so the O(1) running total does not
        # accumulate float drift (keeps parity with summing the entries).
        if self._backend is not None:
            return round(self._shared_totals()[0], 10)
        return round(self._spent, 10)

    @property
    def reserved(self) -> float:
        if self._backend is not None:
            return self._shared_totals()[1]
        with self._lock:
            return self._total_reserved

    @property
    def committed(self) -> float:
        return self.spent + self.reserved

    @property
    def remaining(self) -> float | None:
        if self._budget is None:
            return None
        return max(0.0, self._budget - self.committed)

    @property
    def over_budget(self) -> bool:
        if self._budget is None:
            return False
        return self.spent >= self._budget

    @property
    def warning(self) -> bool:
        if self._budget is None:
            return False
        return self.spent >= self._budget * self._warn_at

    def would_exceed(self, cost: float) -> bool:
        if self._budget is None:
            return False
        if self._backend is not None:
            spent, held, _count = self._shared_totals()
            return spent + held + cost > self._budget
        with self._lock:
            return self.spent + self._total_reserved + cost > self._budget

    def reserve(self, action_id: str, tool_name: str, estimated_cost: float) -> Reservation | None:
        """Reserve budget before execution. Returns None if insufficient budget.

        Raises StateBackendError if a shared backend cannot be reached.
        """
        res = Reservation(
            # Unique per attempt: callers may reuse an action id.
            reservation_id=f"res_{action_id}_{uuid.uuid4().hex[:8]}",
            action_id=action_id,
            tool_name=tool_name,
            estimated_cost=estimated_cost,
        )
        if self._backend is not None:
            self._flush_pending()
            if not self._backend.budget_reserve(
                self._namespace, res.reservation_id, estimated_cost,
                self._budget, self._ttl,
            ):
                return None
            with self._lock:
                self._reservations[res.reservation_id] = res
                self._total_reserved += estimated_cost
            return res
        with self._lock:
            if self._budget is not None:
                if self.spent + self._total_reserved + estimated_cost > self._budget:
                    return None
            self._reservations[res.reservation_id] = res
            self._total_reserved += estimated_cost
            return res

    def commit(self, reservation_id: str, actual_cost: float | None = None) -> CostEntry:
        """Commit a reservation as a real cost entry. Uses actual_cost if provided."""
        with self._lock:
            res = self._reservations.pop(reservation_id, None)
            if res is None:
                raise ValueError(f"Unknown reservation: {reservation_id}")
            self._total_reserved -= res.estimated_cost

            final_cost = actual_cost if actual_cost is not None else res.estimated_cost
            drift = final_cost - res.estimated_cost if actual_cost is not None else 0.0
            self._reconciliation_drift += drift

            entry = CostEntry(
                action_id=res.action_id,
                tool_name=res.tool_name,
                cost=final_cost,
                timestamp=time.time(),
                estimated_cost=res.estimated_cost,
                actual_cost=actual_cost,
                reconciled=actual_cost is not None,
            )
            self._entries.append(entry)
            self._spent += final_cost
        if self._backend is not None:
            self._backend_charge(reservation_id, final_cost)
        return entry

    def release(self, reservation_id: str) -> bool:
        """Release a reservation without recording cost (e.g. action was blocked)."""
        with self._lock:
            res = self._reservations.pop(reservation_id, None)
            if res is None:
                return False
            self._total_reserved -= res.estimated_cost
        if self._backend is not None:
            try:
                self._backend.budget_release(self._namespace, reservation_id)
            except StateBackendError as e:
                # Its time-to-live returns the hold on its own.
                self._report_backend_error(e)
        return True

    def reconcile(self, action_id: str, actual_cost: float) -> CostEntry | None:
        """Update a recorded entry with actual provider charges after the fact."""
        with self._lock:
            for entry in reversed(self._entries):
                if entry.action_id == action_id and not entry.reconciled:
                    drift = actual_cost - entry.cost
                    self._reconciliation_drift += drift
                    self._spent += drift
                    entry.estimated_cost = entry.cost
                    entry.actual_cost = actual_cost
                    entry.cost = actual_cost
                    entry.reconciled = True
                    break
            else:
                return None
        if self._backend is not None:
            self._backend_charge(None, drift)
        return entry

    def record(self, action_id: str, tool_name: str, cost: float) -> CostEntry:
        """Record a cost entry directly (backwards-compatible)."""
        entry = CostEntry(
            action_id=action_id,
            tool_name=tool_name,
            cost=cost,
            timestamp=time.time(),
        )
        with self._lock:
            self._entries.append(entry)
            self._spent += cost
        if self._backend is not None:
            self._backend_charge(None, cost)
        return entry

    def reset(self, keep_reservations: bool = True) -> None:
        """Clear recorded (committed) cost and the running total.

        Active reservations are preserved by default so resetting spend (e.g.
        when rebuilding history from a log) never invalidates an in-flight
        reservation that still needs to be committed.
        """
        with self._lock:
            self._entries.clear()
            self._reconciliation_drift = 0.0
            self._spent = 0.0
            if not keep_reservations:
                self._reservations.clear()
                self._total_reserved = 0.0
        if self._backend is not None:
            self._backend.budget_reset(self._namespace, keep_reservations)

    def summary(self) -> dict:
        degraded = False
        if self._backend is not None:
            try:
                spent, reserved, active = self._shared_totals()
                spent = round(spent, 10)
            except StateBackendError:
                # Reporting must survive an outage. Calls still fail closed; this
                # shows what this worker alone knows about.
                spent, reserved, active = round(self._spent, 10), 0.0, 0
                degraded = True
        else:
            spent, reserved, active = (
                self.spent, self.reserved, len(self._reservations)
            )
        committed = spent + reserved
        remaining = (
            None if self._budget is None else max(0.0, self._budget - committed)
        )
        summary: dict[str, Any] = {
            "budget": self._budget,
            "spent": round(spent, 4),
            "reserved": round(reserved, 4),
            "committed": round(committed, 4),
            "remaining": round(remaining, 4) if remaining is not None else None,
            "entries": len(self._entries),
            "active_reservations": active,
            "reconciliation_drift": round(self._reconciliation_drift, 4),
            "over_budget": self._budget is not None and spent >= self._budget,
            "warning": (
                self._budget is not None and spent >= self._budget * self._warn_at
            ),
        }
        if self._backend is not None:
            summary["shared"] = True
            summary["pending_charges"] = len(self._pending)
        if degraded:
            summary["degraded"] = True
        return summary


def cost_from_usage(
    input_per_mtok: float,
    output_per_mtok: float,
    cached_input_per_mtok: float | None = None,
) -> Callable[[Any], float]:
    """Build an ``actual_cost`` callback that prices a provider's usage report.

    Reads ``usage`` from a tool's output, whether it is an object or a dict and
    whether it uses Anthropic (``input_tokens``) or OpenAI (``prompt_tokens``)
    field names. Prices are per million tokens and are yours to keep current.
    When no usage is present the callback raises and WarderBox keeps the
    declared cost, reported as ``cost_source == "declared"``.

    Usage:
        @shield.tool(cost=0.05, actual_cost=cost_from_usage(3.0, 15.0))
        def call_model(prompt): ...
    """

    def read(source: Any, *names: str) -> float | None:
        for name in names:
            value = (
                source.get(name) if isinstance(source, dict)
                else getattr(source, name, None)
            )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def compute(output: Any) -> float:
        usage = (
            output.get("usage") if isinstance(output, dict)
            else getattr(output, "usage", None)
        )
        if usage is None:
            raise ValueError("output has no usage report")
        inputs = read(usage, "input_tokens", "prompt_tokens")
        outputs = read(usage, "output_tokens", "completion_tokens")
        if inputs is None and outputs is None:
            raise ValueError("usage report has no token counts")
        cached = read(usage, "cache_read_input_tokens", "cached_tokens") or 0.0
        total = (inputs or 0.0) * input_per_mtok + (outputs or 0.0) * output_per_mtok
        if cached_input_per_mtok is not None:
            total += cached * cached_input_per_mtok
        return total / 1_000_000

    return compute
