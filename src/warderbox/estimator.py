"""Pre-execution cost & duration estimation.

Predicts cost and duration BEFORE execution based on declared tool
costs, historical performance data, retry/fallback configuration,
and observed failure rates.

Self-improving: accuracy increases as more executions are recorded.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolStats:
    """Accumulated statistics for a single tool."""

    total_calls: int = 0
    successes: int = 0
    failures: int = 0
    total_cost: float = 0.0
    total_duration: float = 0.0
    costs: list[float] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    retries_used: int = 0
    fallbacks_used: int = 0

    @property
    def failure_rate(self) -> float:
        return (self.failures / self.total_calls) if self.total_calls else 0.0

    @property
    def avg_cost(self) -> float:
        return (self.total_cost / self.total_calls) if self.total_calls else 0.0

    @property
    def avg_duration(self) -> float:
        return (self.total_duration / self.total_calls) if self.total_calls else 0.0

    @property
    def cost_stddev(self) -> float:
        if len(self.costs) < 2:
            return 0.0
        mean = self.avg_cost
        variance = sum((c - mean) ** 2 for c in self.costs) / (len(self.costs) - 1)
        return math.sqrt(variance)

    @property
    def duration_stddev(self) -> float:
        if len(self.durations) < 2:
            return 0.0
        mean = self.avg_duration
        variance = sum((d - mean) ** 2 for d in self.durations) / (len(self.durations) - 1)
        return math.sqrt(variance)

    @property
    def confidence(self) -> float:
        if self.total_calls == 0:
            return 0.0
        if self.total_calls < 5:
            return 0.2
        if self.total_calls < 20:
            return 0.5
        if self.total_calls < 100:
            return 0.8
        return 0.95


@dataclass
class Estimate:
    """Cost and duration estimate for a planned execution."""

    best_cost: float
    expected_cost: float
    worst_cost: float
    best_duration: float
    expected_duration: float
    worst_duration: float
    confidence: float
    data_points: int
    tool_estimates: dict[str, dict[str, float]] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "cost": {
                "best": round(self.best_cost, 4),
                "expected": round(self.expected_cost, 4),
                "worst": round(self.worst_cost, 4),
            },
            "duration_seconds": {
                "best": round(self.best_duration, 2),
                "expected": round(self.expected_duration, 2),
                "worst": round(self.worst_duration, 2),
            },
            "confidence": round(self.confidence, 2),
            "data_points": self.data_points,
            "tools": self.tool_estimates,
        }

    def __repr__(self) -> str:
        return (
            f"Estimate(cost=${self.best_cost:.4f}-${self.worst_cost:.4f}, "
            f"expected=${self.expected_cost:.4f}, "
            f"duration={self.best_duration:.1f}s-{self.worst_duration:.1f}s, "
            f"confidence={self.confidence:.0%})"
        )


class CostEstimator:
    """Predicts execution cost and duration before running.

    Self-improving: gets smarter the more your agents run.

    Usage:
        estimator = CostEstimator()

        # Register declared costs
        estimator.set_tool_cost("gpt4_query", 0.03)
        estimator.set_tool_cost("web_search", 0.01)

        # Record actual executions (WarderBox does this automatically)
        estimator.record("gpt4_query", cost=0.028, duration=1.2, success=True)
        estimator.record("gpt4_query", cost=0.035, duration=2.1, success=True)

        # Estimate before execution
        plan = ["gpt4_query", "web_search", "gpt4_query"]
        estimate = estimator.estimate(plan)
        print(estimate.summary())
    """

    def __init__(self, max_history_per_tool: int = 1000) -> None:
        self._declared_costs: dict[str, float] = {}
        self._declared_durations: dict[str, float] = {}
        self._stats: dict[str, ToolStats] = {}
        self._max_history = max_history_per_tool
        self._lock = threading.Lock()

    def set_tool_cost(self, tool_name: str, cost: float) -> None:
        self._declared_costs[tool_name] = cost

    def set_tool_duration(self, tool_name: str, duration: float) -> None:
        self._declared_durations[tool_name] = duration

    def record(
        self,
        tool_name: str,
        cost: float = 0.0,
        duration: float = 0.0,
        success: bool = True,
        used_retry: bool = False,
        used_fallback: bool = False,
    ) -> None:
        with self._lock:
            if tool_name not in self._stats:
                self._stats[tool_name] = ToolStats()
            stats = self._stats[tool_name]

            stats.total_calls += 1
            if success:
                stats.successes += 1
            else:
                stats.failures += 1

            stats.total_cost += cost
            stats.total_duration += duration

            stats.costs.append(cost)
            stats.durations.append(duration)

            if len(stats.costs) > self._max_history:
                removed_cost = stats.costs.pop(0)
                removed_dur = stats.durations.pop(0)
                stats.total_cost -= removed_cost
                stats.total_duration -= removed_dur
                stats.total_calls -= 1

            if used_retry:
                stats.retries_used += 1
            if used_fallback:
                stats.fallbacks_used += 1

    def estimate(
        self,
        plan: list[str],
        retry_multiplier: float = 1.5,
        fallback_cost_ratio: float = 0.7,
    ) -> Estimate:
        best_cost = 0.0
        expected_cost = 0.0
        worst_cost = 0.0
        best_dur = 0.0
        expected_dur = 0.0
        worst_dur = 0.0
        total_data = 0
        min_confidence = 1.0
        tool_estimates: dict[str, dict[str, float]] = {}

        with self._lock:
            for tool_name in plan:
                stats = self._stats.get(tool_name)
                declared_cost = self._declared_costs.get(tool_name, 0.0)
                declared_dur = self._declared_durations.get(tool_name)

                if stats and stats.total_calls > 0:
                    avg_c = stats.avg_cost
                    std_c = stats.cost_stddev
                    avg_d = stats.avg_duration
                    std_d = stats.duration_stddev
                    fail_rate = stats.failure_rate
                    conf = stats.confidence
                    data = stats.total_calls

                    t_best_c = max(avg_c - std_c, 0.0)
                    t_expected_c = avg_c * (1 + fail_rate * (retry_multiplier - 1))
                    t_worst_c = (avg_c + 2 * std_c) * retry_multiplier

                    t_best_d = max(avg_d - std_d, 0.0)
                    t_expected_d = avg_d * (1 + fail_rate * (retry_multiplier - 1))
                    t_worst_d = (avg_d + 2 * std_d) * retry_multiplier
                else:
                    t_best_c = declared_cost
                    t_expected_c = declared_cost
                    t_worst_c = declared_cost * retry_multiplier
                    conf = 0.0
                    data = 0

                    if declared_dur is not None:
                        t_best_d = declared_dur * 0.5
                        t_expected_d = declared_dur
                        t_worst_d = declared_dur * retry_multiplier
                    else:
                        t_best_d = 0.5
                        t_expected_d = 2.0
                        t_worst_d = 5.0

                best_cost += t_best_c
                expected_cost += t_expected_c
                worst_cost += t_worst_c
                best_dur += t_best_d
                expected_dur += t_expected_d
                worst_dur += t_worst_d
                total_data += data
                min_confidence = min(min_confidence, conf)

                tool_estimates[tool_name] = {
                    "best_cost": round(t_best_c, 4),
                    "expected_cost": round(t_expected_c, 4),
                    "worst_cost": round(t_worst_c, 4),
                    "confidence": round(conf, 2),
                    "data_points": data,
                }

        return Estimate(
            best_cost=best_cost,
            expected_cost=expected_cost,
            worst_cost=worst_cost,
            best_duration=best_dur,
            expected_duration=expected_dur,
            worst_duration=worst_dur,
            confidence=min_confidence if plan else 0.0,
            data_points=total_data,
            tool_estimates=tool_estimates,
        )

    def get_tool_stats(self, tool_name: str) -> dict | None:
        with self._lock:
            stats = self._stats.get(tool_name)
            if stats is None:
                return None
            return {
                "total_calls": stats.total_calls,
                "successes": stats.successes,
                "failures": stats.failures,
                "failure_rate": round(stats.failure_rate, 3),
                "avg_cost": round(stats.avg_cost, 4),
                "avg_duration": round(stats.avg_duration, 3),
                "cost_stddev": round(stats.cost_stddev, 4),
                "duration_stddev": round(stats.duration_stddev, 3),
                "confidence": round(stats.confidence, 2),
                "retries_used": stats.retries_used,
                "fallbacks_used": stats.fallbacks_used,
            }

    def stats(self) -> dict:
        with self._lock:
            total_data = sum(s.total_calls for s in self._stats.values())
            return {
                "tracked_tools": len(self._stats),
                "total_data_points": total_data,
                "tools": {
                    name: {
                        "calls": s.total_calls,
                        "confidence": round(s.confidence, 2),
                        "avg_cost": round(s.avg_cost, 4),
                    }
                    for name, s in self._stats.items()
                },
            }

    def reset(self, tool_name: str | None = None) -> None:
        with self._lock:
            if tool_name:
                self._stats.pop(tool_name, None)
            else:
                self._stats.clear()
