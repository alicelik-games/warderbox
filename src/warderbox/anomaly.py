"""Cost Anomaly Detection — catch runaway agents and unexpected spending spikes.

Monitors agent spending patterns and fires alerts when costs deviate
from the established baseline.

Usage:
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    shield.set_anomaly_detector(detector)

    # Or standalone:
    detector.record(tool="search_api", cost=0.03)
    detector.record(tool="search_api", cost=0.03)
    detector.record(tool="search_api", cost=5.00)  # spike!
    alert = detector.check()
    # → AnomalyAlert(type="cost_spike", ...)
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class AnomalyType(Enum):
    COST_SPIKE = "cost_spike"
    RATE_SPIKE = "rate_spike"
    BUDGET_BURN = "budget_burn"
    LOOP_DETECTED = "loop_detected"
    TOOL_COST_SPIKE = "tool_cost_spike"


@dataclass
class AnomalyAlert:
    type: AnomalyType
    severity: str
    message: str
    metric_value: float
    threshold: float
    tool_name: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "severity": self.severity,
            "message": self.message,
            "metric_value": round(self.metric_value, 4),
            "threshold": round(self.threshold, 4),
            "tool_name": self.tool_name,
            "timestamp": self.timestamp,
        }


@dataclass
class CostRecord:
    tool_name: str
    cost: float
    timestamp: float = field(default_factory=time.time)


class AnomalyDetector:
    """Detects anomalous spending patterns in real time.

    Uses a rolling window with z-score based detection for cost spikes,
    rate monitoring for runaway loops, and per-tool tracking.
    """

    def __init__(
        self,
        window_size: int = 100,
        sensitivity: float = 2.0,
        rate_window_seconds: float = 60.0,
        max_rate_per_minute: int | None = None,
        loop_threshold: int = 10,
        burn_rate_multiplier: float = 3.0,
        on_alert: Callable[[AnomalyAlert], None] | None = None,
    ) -> None:
        self._window_size = window_size
        self._sensitivity = sensitivity
        self._rate_window = rate_window_seconds
        self._max_rate = max_rate_per_minute
        self._loop_threshold = loop_threshold
        self._burn_multiplier = burn_rate_multiplier
        self._on_alert = on_alert

        self._records: deque[CostRecord] = deque(maxlen=window_size * 2)
        self._alerts: deque[AnomalyAlert] = deque(maxlen=1000)
        self._tool_costs: dict[str, list[float]] = {}
        self._recent_tools: deque[str] = deque(maxlen=loop_threshold * 2)
        self._baseline_burn_rate: float | None = None
        self._lock = threading.RLock()

    @property
    def alerts(self) -> list[AnomalyAlert]:
        with self._lock:
            return list(self._alerts)

    def set_callback(self, fn: Callable[[AnomalyAlert], None]) -> None:
        self._on_alert = fn

    def set_baseline_burn_rate(self, rate_per_minute: float) -> None:
        self._baseline_burn_rate = rate_per_minute

    def record(self, tool_name: str, cost: float, timestamp: float | None = None) -> list[AnomalyAlert]:
      with self._lock:
        ts = timestamp or time.time()
        rec = CostRecord(tool_name=tool_name, cost=cost, timestamp=ts)
        self._records.append(rec)

        if tool_name not in self._tool_costs:
            self._tool_costs[tool_name] = []
        self._tool_costs[tool_name].append(cost)
        if len(self._tool_costs[tool_name]) > self._window_size * 2:
            self._tool_costs[tool_name] = self._tool_costs[tool_name][-self._window_size:]

        self._recent_tools.append(tool_name)

        new_alerts = []

        alert = self._check_cost_spike(cost)
        if alert:
            new_alerts.append(alert)

        alert = self._check_tool_cost_spike(tool_name, cost)
        if alert:
            new_alerts.append(alert)

        alert = self._check_rate_spike(ts)
        if alert:
            new_alerts.append(alert)

        alert = self._check_loop()
        if alert:
            new_alerts.append(alert)

        alert = self._check_burn_rate(ts)
        if alert:
            new_alerts.append(alert)

        for a in new_alerts:
            self._alerts.append(a)
            if self._on_alert:
                try:
                    self._on_alert(a)
                except Exception:
                    pass  # a faulty alert callback must not break detection

        return new_alerts

    def check(self) -> list[AnomalyAlert]:
      with self._lock:
        if not self._records:
            return []
        last = self._records[-1]
        alerts = []

        alert = self._check_cost_spike(last.cost)
        if alert:
            alerts.append(alert)

        alert = self._check_burn_rate(last.timestamp)
        if alert:
            alerts.append(alert)

        return alerts

    def stats(self) -> dict:
      with self._lock:
        costs = [r.cost for r in self._records]
        return {
            "total_records": len(self._records),
            "total_alerts": len(self._alerts),
            "alerts_by_type": self._count_by_type(),
            "mean_cost": self._mean(costs),
            "std_cost": self._std(costs),
            "window_size": self._window_size,
            "sensitivity": self._sensitivity,
        }

    def reset(self) -> None:
        self._records.clear()
        self._alerts.clear()
        self._tool_costs.clear()
        self._recent_tools.clear()
        self._baseline_burn_rate = None

    def _check_cost_spike(self, cost: float) -> AnomalyAlert | None:
        costs = [r.cost for r in self._records]
        if len(costs) < 5:
            return None

        window = costs[-self._window_size - 1:-1] if len(costs) > 1 else costs
        if not window:
            return None

        mean = self._mean(window)
        std = self._std(window)

        if std == 0:
            if cost > mean * 2 and cost > 0 and mean > 0:
                return AnomalyAlert(
                    type=AnomalyType.COST_SPIKE,
                    severity="high",
                    message=f"Cost ${cost:.4f} is {cost / mean:.1f}x the average ${mean:.4f}",
                    metric_value=cost,
                    threshold=mean * 2,
                )
            return None

        z_score = (cost - mean) / std
        if z_score > self._sensitivity:
            severity = "critical" if z_score > self._sensitivity * 2 else "high"
            return AnomalyAlert(
                type=AnomalyType.COST_SPIKE,
                severity=severity,
                message=f"Cost ${cost:.4f} is {z_score:.1f} std devs above mean ${mean:.4f}",
                metric_value=cost,
                threshold=mean + std * self._sensitivity,
            )
        return None

    def _check_tool_cost_spike(self, tool_name: str, cost: float) -> AnomalyAlert | None:
        history = self._tool_costs.get(tool_name, [])
        if len(history) < 5:
            return None

        window = history[-self._window_size - 1:-1]
        if not window:
            return None

        mean = self._mean(window)
        std = self._std(window)

        if std == 0:
            if cost > mean * 3 and cost > 0 and mean > 0:
                return AnomalyAlert(
                    type=AnomalyType.TOOL_COST_SPIKE,
                    severity="high",
                    message=f"Tool '{tool_name}' cost ${cost:.4f} is {cost / mean:.1f}x its average ${mean:.4f}",
                    metric_value=cost,
                    threshold=mean * 3,
                    tool_name=tool_name,
                )
            return None

        z_score = (cost - mean) / std
        if z_score > self._sensitivity:
            return AnomalyAlert(
                type=AnomalyType.TOOL_COST_SPIKE,
                severity="high",
                message=f"Tool '{tool_name}' cost ${cost:.4f} is {z_score:.1f} std devs above its mean ${mean:.4f}",
                metric_value=cost,
                threshold=mean + std * self._sensitivity,
                tool_name=tool_name,
            )
        return None

    def _check_rate_spike(self, now: float) -> AnomalyAlert | None:
        if self._max_rate is None:
            return None

        cutoff = now - self._rate_window
        recent = sum(1 for r in self._records if r.timestamp > cutoff)
        rate_per_min = recent / (self._rate_window / 60.0)

        if rate_per_min > self._max_rate:
            return AnomalyAlert(
                type=AnomalyType.RATE_SPIKE,
                severity="high",
                message=f"Action rate {rate_per_min:.0f}/min exceeds limit {self._max_rate}/min",
                metric_value=rate_per_min,
                threshold=float(self._max_rate),
            )
        return None

    def _check_loop(self) -> AnomalyAlert | None:
        if len(self._recent_tools) < self._loop_threshold:
            return None

        recent = list(self._recent_tools)[-self._loop_threshold:]
        if len(set(recent)) == 1:
            tool = recent[0]
            return AnomalyAlert(
                type=AnomalyType.LOOP_DETECTED,
                severity="critical",
                message=f"Possible infinite loop: '{tool}' called {self._loop_threshold} times consecutively",
                metric_value=float(self._loop_threshold),
                threshold=float(self._loop_threshold),
                tool_name=tool,
            )
        return None

    def _check_burn_rate(self, now: float) -> AnomalyAlert | None:
        if self._baseline_burn_rate is None:
            return None

        cutoff = now - 60.0
        recent_costs = [r.cost for r in self._records if r.timestamp > cutoff]
        if not recent_costs:
            return None

        current_burn = sum(recent_costs)
        threshold = self._baseline_burn_rate * self._burn_multiplier

        if current_burn > threshold:
            return AnomalyAlert(
                type=AnomalyType.BUDGET_BURN,
                severity="high",
                message=f"Burn rate ${current_burn:.4f}/min is {current_burn / self._baseline_burn_rate:.1f}x baseline ${self._baseline_burn_rate:.4f}/min",
                metric_value=current_burn,
                threshold=threshold,
            )
        return None

    def _count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self._alerts:
            counts[a.type.value] = counts.get(a.type.value, 0) + 1
        return counts

    @staticmethod
    def _mean(values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)

    @staticmethod
    def _std(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)
