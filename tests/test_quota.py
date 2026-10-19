"""Tests for execution quotas."""

import time
from warderbox.quota import QuotaManager, QuotaPeriod


def test_no_quota_allows():
    qm = QuotaManager()
    exceeded, reason = qm.would_exceed("any_tool")
    assert exceeded is False
    assert reason is None


def test_daily_quota():
    qm = QuotaManager()
    qm.set_quota("send_email", QuotaPeriod.DAILY, 3)
    qm.record("send_email")
    qm.record("send_email")
    qm.record("send_email")
    exceeded, reason = qm.would_exceed("send_email")
    assert exceeded is True
    assert "3/3" in reason


def test_quota_not_exceeded():
    qm = QuotaManager()
    qm.set_quota("tool_a", QuotaPeriod.DAILY, 10)
    qm.record("tool_a")
    exceeded, _ = qm.would_exceed("tool_a")
    assert exceeded is False


def test_weekly_quota():
    qm = QuotaManager()
    qm.set_quota("api_call", QuotaPeriod.WEEKLY, 2)
    qm.record("api_call")
    qm.record("api_call")
    exceeded, reason = qm.would_exceed("api_call")
    assert exceeded is True
    assert "weekly" in reason


def test_monthly_quota():
    qm = QuotaManager()
    qm.set_quota("expensive_tool", QuotaPeriod.MONTHLY, 100)
    for _ in range(100):
        qm.record("expensive_tool")
    exceeded, _ = qm.would_exceed("expensive_tool")
    assert exceeded is True


def test_global_quota():
    qm = QuotaManager()
    qm.set_quota("global", QuotaPeriod.DAILY, 5)
    for _ in range(5):
        qm.record("tool_a")
    exceeded, reason = qm.would_exceed("tool_b")
    assert exceeded is True
    assert "global" in reason


def test_remove_quota():
    qm = QuotaManager()
    qm.set_quota("a", QuotaPeriod.DAILY, 1)
    qm.record("a")
    assert qm.would_exceed("a")[0] is True
    qm.remove_quota("a")
    assert qm.would_exceed("a")[0] is False


def test_remove_specific_period():
    qm = QuotaManager()
    qm.set_quota("a", QuotaPeriod.DAILY, 1)
    qm.set_quota("a", QuotaPeriod.MONTHLY, 100)
    qm.remove_quota("a", QuotaPeriod.DAILY)
    usage = qm.get_usage("a")
    assert len(usage) == 1
    assert usage[0]["period"] == "monthly"


def test_remove_nonexistent():
    qm = QuotaManager()
    assert qm.remove_quota("nope") is False


def test_get_usage():
    qm = QuotaManager()
    qm.set_quota("tool_a", QuotaPeriod.DAILY, 10)
    qm.record("tool_a")
    qm.record("tool_a")
    usage = qm.get_usage("tool_a")
    assert len(usage) == 1
    assert usage[0]["current_calls"] == 2
    assert usage[0]["remaining"] == 8
    assert usage[0]["max_calls"] == 10


def test_get_usage_empty():
    qm = QuotaManager()
    assert qm.get_usage("nope") == []


def test_multiple_periods_same_scope():
    qm = QuotaManager()
    qm.set_quota("api", QuotaPeriod.DAILY, 100)
    qm.set_quota("api", QuotaPeriod.MONTHLY, 1000)
    usage = qm.get_usage("api")
    assert len(usage) == 2


def test_stats():
    qm = QuotaManager()
    qm.set_quota("a", QuotaPeriod.DAILY, 5)
    qm.set_quota("b", QuotaPeriod.WEEKLY, 50)
    for _ in range(5):
        qm.record("a")
    stats = qm.stats()
    assert stats["total_quotas"] == 2
    assert stats["scopes_tracked"] == 2
    assert stats["currently_exceeded"] == 1


def test_utilization():
    qm = QuotaManager()
    qm.set_quota("tool", QuotaPeriod.DAILY, 10)
    for _ in range(5):
        qm.record("tool")
    usage = qm.get_usage("tool")
    assert usage[0]["utilization"] == 0.5


def test_replace_quota_same_period():
    qm = QuotaManager()
    qm.set_quota("a", QuotaPeriod.DAILY, 5)
    qm.set_quota("a", QuotaPeriod.DAILY, 100)
    usage = qm.get_usage("a")
    assert len(usage) == 1
    assert usage[0]["max_calls"] == 100
