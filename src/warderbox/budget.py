"""Budget tracker for agent cost control.

Supports thread-safe cost reservation and post-execution reconciliation
with actual provider charges.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


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
    workers cannot overshoot the budget.
    """

    def __init__(self, budget: float | None = None, warn_at: float = 0.8) -> None:
        self._budget = budget
        self._warn_at = warn_at
        self._entries: list[CostEntry] = []
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.Lock()
        self._total_reserved: float = 0.0
        self._reconciliation_drift: float = 0.0

    @property
    def budget(self) -> float | None:
        return self._budget

    @property
    def spent(self) -> float:
        return sum(e.cost for e in self._entries)

    @property
    def reserved(self) -> float:
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
        with self._lock:
            return self.spent + self._total_reserved + cost > self._budget

    def reserve(self, action_id: str, tool_name: str, estimated_cost: float) -> Reservation | None:
        """Reserve budget before execution. Returns None if insufficient budget."""
        with self._lock:
            if self._budget is not None:
                if self.spent + self._total_reserved + estimated_cost > self._budget:
                    return None
            res = Reservation(
                reservation_id=f"res_{action_id}",
                action_id=action_id,
                tool_name=tool_name,
                estimated_cost=estimated_cost,
            )
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
            return entry

    def release(self, reservation_id: str) -> bool:
        """Release a reservation without recording cost (e.g. action was blocked)."""
        with self._lock:
            res = self._reservations.pop(reservation_id, None)
            if res is None:
                return False
            self._total_reserved -= res.estimated_cost
            return True

    def reconcile(self, action_id: str, actual_cost: float) -> CostEntry | None:
        """Update a recorded entry with actual provider charges after the fact."""
        with self._lock:
            for entry in reversed(self._entries):
                if entry.action_id == action_id and not entry.reconciled:
                    drift = actual_cost - entry.cost
                    self._reconciliation_drift += drift
                    entry.estimated_cost = entry.cost
                    entry.actual_cost = actual_cost
                    entry.cost = actual_cost
                    entry.reconciled = True
                    return entry
            return None

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
        return entry

    def summary(self) -> dict:
        return {
            "budget": self._budget,
            "spent": round(self.spent, 4),
            "reserved": round(self.reserved, 4),
            "committed": round(self.committed, 4),
            "remaining": round(self.remaining, 4) if self.remaining is not None else None,
            "entries": len(self._entries),
            "active_reservations": len(self._reservations),
            "reconciliation_drift": round(self._reconciliation_drift, 4),
            "over_budget": self.over_budget,
            "warning": self.warning,
        }
