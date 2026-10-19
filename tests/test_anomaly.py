"""Tests for cost anomaly detection."""

import time
import pytest
from warderbox.anomaly import AnomalyDetector, AnomalyType, AnomalyAlert


def test_no_alerts_on_normal_costs():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0, loop_threshold=100)
    for _ in range(20):
        alerts = detector.record("search", 0.03)
    assert len(alerts) == 0
    assert len(detector.alerts) == 0


def test_cost_spike_detected():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    for _ in range(20):
        detector.record("search", 0.03)
    alerts = detector.record("search", 5.00)
    assert len(alerts) > 0
    assert any(a.type == AnomalyType.COST_SPIKE for a in alerts)


def test_cost_spike_severity():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0, loop_threshold=100)
    for _ in range(20):
        detector.record("api", 0.01)
    alerts = detector.record("api", 100.0)
    spike_alerts = [a for a in alerts if a.type == AnomalyType.COST_SPIKE]
    assert spike_alerts[0].severity in ("high", "critical")


def test_tool_cost_spike():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    for _ in range(10):
        detector.record("db_query", 0.05)
    alerts = detector.record("db_query", 10.0)
    tool_alerts = [a for a in alerts if a.type == AnomalyType.TOOL_COST_SPIKE]
    assert len(tool_alerts) > 0
    assert tool_alerts[0].tool_name == "db_query"


def test_loop_detected():
    detector = AnomalyDetector(loop_threshold=5)
    alerts = []
    for _ in range(10):
        result = detector.record("stuck_tool", 0.01)
        alerts.extend(result)
    loop_alerts = [a for a in alerts if a.type == AnomalyType.LOOP_DETECTED]
    assert len(loop_alerts) > 0
    assert loop_alerts[0].tool_name == "stuck_tool"
    assert loop_alerts[0].severity == "critical"


def test_no_loop_with_varied_tools():
    detector = AnomalyDetector(loop_threshold=5)
    tools = ["search", "db", "email", "search", "db"]
    alerts = []
    for t in tools:
        result = detector.record(t, 0.01)
        alerts.extend(result)
    loop_alerts = [a for a in alerts if a.type == AnomalyType.LOOP_DETECTED]
    assert len(loop_alerts) == 0


def test_rate_spike():
    detector = AnomalyDetector(max_rate_per_minute=10, rate_window_seconds=60.0)
    now = time.time()
    alerts = []
    for i in range(15):
        result = detector.record("api", 0.01, timestamp=now + i * 0.1)
        alerts.extend(result)
    rate_alerts = [a for a in alerts if a.type == AnomalyType.RATE_SPIKE]
    assert len(rate_alerts) > 0


def test_no_rate_spike_under_limit():
    detector = AnomalyDetector(max_rate_per_minute=100, rate_window_seconds=60.0)
    now = time.time()
    for i in range(5):
        alerts = detector.record("api", 0.01, timestamp=now + i * 15)
    assert not any(a.type == AnomalyType.RATE_SPIKE for a in alerts)


def test_burn_rate_alert():
    detector = AnomalyDetector(burn_rate_multiplier=2.0)
    detector.set_baseline_burn_rate(0.10)
    now = time.time()
    alerts = []
    for i in range(20):
        result = detector.record("expensive", 0.50, timestamp=now + i * 0.5)
        alerts.extend(result)
    burn_alerts = [a for a in alerts if a.type == AnomalyType.BUDGET_BURN]
    assert len(burn_alerts) > 0


def test_no_burn_alert_without_baseline():
    detector = AnomalyDetector()
    for _ in range(20):
        alerts = detector.record("tool", 1.00)
    burn_alerts = [a for a in alerts if a.type == AnomalyType.BUDGET_BURN]
    assert len(burn_alerts) == 0


def test_callback_fired():
    received = []
    detector = AnomalyDetector(
        window_size=50,
        sensitivity=2.0,
        on_alert=lambda a: received.append(a),
    )
    for _ in range(20):
        detector.record("tool", 0.01)
    detector.record("tool", 50.0)
    assert len(received) > 0


def test_set_callback():
    received = []
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    detector.set_callback(lambda a: received.append(a))
    for _ in range(20):
        detector.record("tool", 0.01)
    detector.record("tool", 50.0)
    assert len(received) > 0


def test_stats():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    for _ in range(10):
        detector.record("tool", 0.05)
    detector.record("tool", 100.0)

    stats = detector.stats()
    assert stats["total_records"] == 11
    assert stats["total_alerts"] > 0
    assert "mean_cost" in stats
    assert "std_cost" in stats


def test_reset():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    for _ in range(10):
        detector.record("tool", 0.05)
    detector.record("tool", 100.0)
    detector.reset()

    assert detector.stats()["total_records"] == 0
    assert detector.stats()["total_alerts"] == 0


def test_alert_to_dict():
    alert = AnomalyAlert(
        type=AnomalyType.COST_SPIKE,
        severity="high",
        message="Cost spike detected",
        metric_value=5.0,
        threshold=1.0,
        tool_name="search",
    )
    d = alert.to_dict()
    assert d["type"] == "cost_spike"
    assert d["severity"] == "high"
    assert d["tool_name"] == "search"


def test_check_method():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    for _ in range(20):
        detector.record("tool", 0.01)
    detector.record("tool", 50.0)
    alerts = detector.check()
    assert isinstance(alerts, list)


def test_check_empty():
    detector = AnomalyDetector()
    alerts = detector.check()
    assert alerts == []


def test_sensitivity_affects_detection():
    low_sens = AnomalyDetector(sensitivity=1.0)
    high_sens = AnomalyDetector(sensitivity=5.0)

    for _ in range(20):
        low_sens.record("tool", 0.03)
        high_sens.record("tool", 0.03)

    low_alerts = low_sens.record("tool", 0.15)
    high_alerts = high_sens.record("tool", 0.15)

    assert len(low_alerts) >= len(high_alerts)


def test_multiple_anomaly_types_at_once():
    detector = AnomalyDetector(
        window_size=50,
        sensitivity=2.0,
        loop_threshold=5,
    )
    for _ in range(20):
        detector.record("stuck", 0.01)
    alerts = detector.record("stuck", 50.0)
    types = {a.type for a in alerts}
    assert AnomalyType.COST_SPIKE in types or AnomalyType.LOOP_DETECTED in types


def test_alerts_by_type_count():
    detector = AnomalyDetector(window_size=50, sensitivity=2.0)
    for _ in range(10):
        detector.record("tool", 0.01)
    detector.record("tool", 100.0)
    detector.record("tool", 100.0)

    stats = detector.stats()
    assert sum(stats["alerts_by_type"].values()) == stats["total_alerts"]
