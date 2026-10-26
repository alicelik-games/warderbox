"""Cost forecasting and budget burn-rate analysis.

Predicts when the budget will run out based on historical usage patterns.

Usage:
    forecaster = CostForecaster(shield)
    report = forecaster.forecast()
    print(report.burn_rate_per_minute)
    print(report.estimated_exhaustion)  # datetime or None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class CostForecast:
    total_spent: float
    budget: float | None
    remaining: float | None
    total_actions: int
    time_window_seconds: float
    burn_rate_per_minute: float
    burn_rate_per_hour: float
    avg_cost_per_action: float
    estimated_exhaustion: datetime | None
    estimated_actions_remaining: int | None
    cost_by_tool: dict[str, float] = field(default_factory=dict)
    trend: str = "stable"

    @property
    def summary(self) -> str:
        lines = [
            "=== Cost Forecast ===",
            f"Spent: ${self.total_spent:.2f}",
        ]
        if self.budget is not None:
            lines.append(f"Budget: ${self.budget:.2f}")
            lines.append(f"Remaining: ${self.remaining:.2f}")
        lines.extend([
            f"Actions: {self.total_actions}",
            f"Avg cost/action: ${self.avg_cost_per_action:.4f}",
            f"Burn rate: ${self.burn_rate_per_hour:.2f}/hr",
            f"Trend: {self.trend}",
        ])
        if self.estimated_exhaustion:
            lines.append(f"Estimated exhaustion: {self.estimated_exhaustion.isoformat()}")
        if self.estimated_actions_remaining is not None:
            lines.append(f"Estimated actions remaining: {self.estimated_actions_remaining}")
        if self.cost_by_tool:
            lines.append("\nCost by tool:")
            for tool, cost in sorted(self.cost_by_tool.items(), key=lambda x: -x[1]):
                lines.append(f"  {tool}: ${cost:.4f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_spent": self.total_spent,
            "budget": self.budget,
            "remaining": self.remaining,
            "total_actions": self.total_actions,
            "burn_rate_per_minute": round(self.burn_rate_per_minute, 6),
            "burn_rate_per_hour": round(self.burn_rate_per_hour, 4),
            "avg_cost_per_action": round(self.avg_cost_per_action, 6),
            "estimated_exhaustion": (
                self.estimated_exhaustion.isoformat()
                if self.estimated_exhaustion else None
            ),
            "estimated_actions_remaining": self.estimated_actions_remaining,
            "cost_by_tool": self.cost_by_tool,
            "trend": self.trend,
        }


class CostForecaster:
    """Analyzes spending patterns and predicts budget exhaustion."""

    def __init__(self, shield, min_window_seconds: float = 60.0) -> None:
        self._shield = shield
        self._min_window_seconds = min_window_seconds

    def forecast(self) -> CostForecast:
        history = self._shield.history()

        if not history:
            budget = self._shield._budget.budget
            return CostForecast(
                total_spent=0.0,
                budget=budget,
                remaining=budget,
                total_actions=0,
                time_window_seconds=0.0,
                burn_rate_per_minute=0.0,
                burn_rate_per_hour=0.0,
                avg_cost_per_action=0.0,
                estimated_exhaustion=None,
                estimated_actions_remaining=None,
            )

        total_spent = self._shield._budget.spent
        budget = self._shield._budget.budget
        remaining = budget - total_spent if budget else None

        timestamps = [h["timestamp"] for h in history]
        costs = [h["cost"] or 0.0 for h in history]

        first_ts = min(timestamps)
        last_ts = max(timestamps)

        first_time = self._parse_ts(first_ts)
        last_time = self._parse_ts(last_ts)
        observed = (last_time - first_time).total_seconds()
        # Floor the observation window so a burst of fast calls doesn't produce
        # an absurd burn rate (dividing cost by a microsecond window).
        window = max(observed, self._min_window_seconds)

        # Use the budget's running total (already rounded) rather than
        # re-summing the per-action costs: this keeps the figures consistent
        # with reported spend and stable across Python versions (3.12+ changed
        # float sum() to compensated summation, which shifted the last digit).
        total_cost_in_window = total_spent
        burn_per_sec = total_cost_in_window / window if window > 0 else 0.0
        burn_per_min = burn_per_sec * 60
        burn_per_hour = burn_per_sec * 3600

        avg_cost = round(total_spent / len(costs), 10) if costs else 0.0

        exhaustion = None
        actions_remaining = None
        if budget and remaining is not None and remaining > 0:
            if burn_per_sec > 0:
                secs_left = remaining / burn_per_sec
                exhaustion = datetime.now() + timedelta(seconds=secs_left)
            if avg_cost > 0:
                actions_remaining = int(remaining / avg_cost)

        cost_by_tool: dict[str, float] = {}
        for h in history:
            # Attribute a fallback call to the tool that actually ran.
            tool = h.get("fallback") or h["tool"]
            cost_by_tool[tool] = cost_by_tool.get(tool, 0.0) + (h["cost"] or 0.0)

        trend = self._detect_trend(history)

        return CostForecast(
            total_spent=total_spent,
            budget=budget,
            remaining=remaining,
            total_actions=len(history),
            time_window_seconds=window,
            burn_rate_per_minute=burn_per_min,
            burn_rate_per_hour=burn_per_hour,
            avg_cost_per_action=avg_cost,
            estimated_exhaustion=exhaustion,
            estimated_actions_remaining=actions_remaining,
            cost_by_tool=cost_by_tool,
            trend=trend,
        )

    def _detect_trend(self, history: list[dict]) -> str:
        if len(history) < 4:
            return "insufficient_data"

        costs = [h["cost"] or 0.0 for h in history]
        mid = len(costs) // 2
        first_half_avg = sum(costs[:mid]) / mid
        second_half_avg = sum(costs[mid:]) / (len(costs) - mid)

        if second_half_avg > first_half_avg * 1.2:
            return "increasing"
        if second_half_avg < first_half_avg * 0.8:
            return "decreasing"
        return "stable"

    @staticmethod
    def _parse_ts(ts: str | float) -> datetime:
        if isinstance(ts, (int, float)):
            try:
                return datetime.fromtimestamp(ts)
            except (ValueError, OSError, OverflowError):
                return datetime.now()
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return datetime.now()
