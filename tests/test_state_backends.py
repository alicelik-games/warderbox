"""Shared limits hold across real worker processes."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import time

import pytest

from warderbox import (
    QuotaManager,
    QuotaPeriod,
    SQLiteStateBackend,
    ToolLimit,
    WarderBox,
    load_policy,
    validate_policy,
)
from warderbox.budget import BudgetTracker
from warderbox.ratelimit import RateLimiter
from warderbox.state import RedisStateBackend, StateBackendError, backend_from_config


def _make_sqlite(tmp_path):
    return SQLiteStateBackend(tmp_path / "state.db")


def _make_redis(tmp_path):
    # Point this at a disposable server to run the suite against real Redis.
    # Its database is flushed.
    url = os.environ.get("WARDERBOX_TEST_REDIS_URL")
    if url:
        redis = pytest.importorskip("redis")
        client = redis.Redis.from_url(url)
        client.flushdb()
        return RedisStateBackend(client=client)
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa")
    return RedisStateBackend(client=fakeredis.FakeRedis())


@pytest.fixture(params=[_make_sqlite, _make_redis], ids=["sqlite", "redis"])
def backend(request, tmp_path):
    instance = request.param(tmp_path)
    yield instance
    instance.close()


def test_budget_is_shared_between_instances(backend):
    a = BudgetTracker(budget=1.0, backend=backend)
    b = BudgetTracker(budget=1.0, backend=backend)
    first = a.reserve("1", "t", 0.6)
    assert first is not None
    assert b.reserve("2", "t", 0.6) is None  # the hold is visible to b
    assert b.summary()["reserved"] == 0.6
    a.commit(first.reservation_id, 0.5)
    assert b.spent == 0.5
    second = b.reserve("2", "t", 0.5)
    assert second is not None
    assert b.release(second.reservation_id)
    assert a.reserved == 0


def test_budget_namespaces_are_isolated(backend):
    a = BudgetTracker(budget=1.0, backend=backend, namespace="a")
    b = BudgetTracker(budget=1.0, backend=backend, namespace="b")
    a.commit(a.reserve("1", "t", 1.0).reservation_id)
    assert b.spent == 0
    assert b.reserve("2", "t", 1.0) is not None


def test_budget_float_totals_do_not_block_a_valid_last_call(backend):
    tracker = BudgetTracker(budget=1.0, backend=backend)
    for i in range(10):
        reservation = tracker.reserve(str(i), "t", 0.1)
        assert reservation is not None, i
        tracker.commit(reservation.reservation_id)
    assert tracker.reserve("x", "t", 0.1) is None


def test_crashed_workers_reservation_expires(backend):
    crashed = BudgetTracker(budget=1.0, backend=backend, reservation_ttl=0.05)
    assert crashed.reserve("1", "t", 1.0) is not None
    other = BudgetTracker(budget=1.0, backend=backend)
    assert other.reserve("2", "t", 1.0) is None
    time.sleep(0.1)
    assert other.reserve("2", "t", 1.0) is not None


def test_commit_after_expiry_still_counts_the_spend(backend):
    tracker = BudgetTracker(budget=1.0, backend=backend, reservation_ttl=0.01)
    reservation = tracker.reserve("1", "t", 0.4)
    time.sleep(0.05)
    tracker.commit(reservation.reservation_id)
    assert tracker.spent == 0.4


def test_reconcile_record_and_reset(backend):
    tracker = BudgetTracker(budget=5.0, backend=backend)
    tracker.commit(tracker.reserve("1", "t", 1.0).reservation_id)
    tracker.reconcile("1", 2.5)
    tracker.record("2", "t", 0.5)
    assert BudgetTracker(budget=5.0, backend=backend).spent == 3.0
    tracker.reset()
    assert BudgetTracker(budget=5.0, backend=backend).spent == 0


def test_rate_limit_is_shared(backend):
    a = RateLimiter(tool_limits={"t": ToolLimit(rpm=2)}, global_rpm=3, backend=backend)
    b = RateLimiter(tool_limits={"t": ToolLimit(rpm=2)}, global_rpm=3, backend=backend)
    assert a.reserve("t")[0]
    assert b.reserve("t")[0]
    allowed, reason = a.reserve("t")
    assert not allowed and "rate limit exceeded (2/min)" in reason
    assert b.would_exceed("t")[0]
    b.release("t")
    assert a.reserve("t")[0]
    assert a.reserve("other")[0]
    allowed, reason = b.reserve("other")
    assert not allowed and "Global" in reason
    stats = b.stats()
    assert stats["global_last_minute"] == 3
    assert stats["by_tool"]["t"]["last_minute"] == 2


def test_rate_window_expires(backend):
    limiter = RateLimiter(backend=backend)
    limiter.record("t")
    assert backend.window_count("default", "tool:t", 60) == 1
    assert backend.window_count("default", "tool:t", 0.0) == 0


def test_quota_is_shared(backend):
    a, b = QuotaManager(backend=backend), QuotaManager(backend=backend)
    for manager in (a, b):
        manager.set_quota("t", QuotaPeriod.DAILY, 2)
    assert a.reserve("t")[0]
    assert b.reserve("t")[0]
    allowed, reason = a.reserve("t")
    assert not allowed and "2/2 daily" in reason
    assert b.would_exceed("t")[0]
    assert a.get_usage("t")[0]["current_calls"] == 2
    b.release("t")
    assert a.reserve("t")[0]
    assert a.stats()["currently_exceeded"] == 1


def test_quota_period_elapses(backend):
    assert backend.counter_reserve("n", [("k", 1, 0.05)]) is None
    assert backend.counter_reserve("n", [("k", 1, 0.05)]) == (0, 1)
    time.sleep(0.1)
    assert backend.counter_reserve("n", [("k", 1, 0.05)]) is None


def test_quota_all_or_nothing(backend):
    checks = [("a", 5, 60.0), ("b", 0, 60.0)]
    assert backend.counter_reserve("n", checks) == (1, 0)
    assert backend.counter_get("n", "a", 60.0)[0] == 0


def test_shield_with_shared_backend_shares_everything(backend):
    def make():
        shield = WarderBox(
            budget=1.0, state_backend=backend, namespace="prod",
            tool_limits={"tool": ToolLimit(rpm=100)},
        )
        quota = QuotaManager()
        quota.set_quota("tool", QuotaPeriod.DAILY, 100)
        shield.set_quota(quota)

        @shield.tool(cost=0.4)
        def tool():
            return "x"

        return shield, tool

    (s1, t1), (s2, t2) = make(), make()
    assert t1().success
    assert t2().success
    third = t1()
    assert third.blocked and "Budget exceeded" in third.block_reason
    assert s2.budget_summary["spent"] == 0.8
    assert s2.budget_summary["shared"] is True
    assert s1._quota.get_usage("tool")[0]["current_calls"] == 2


def test_test_mode_leaves_shared_state_untouched(backend):
    shield = WarderBox(
        budget=1.0, state_backend=backend, mode="test",
        tool_limits={"tool": ToolLimit(rpm=1)},
    )

    @shield.tool(cost=0.4)
    def tool():
        return "x"

    for _ in range(3):
        assert tool().success
    assert shield.budget_summary["spent"] == 0
    assert shield.budget_summary["active_reservations"] == 0
    assert backend.window_count("default", "tool:tool", 60) == 0


def test_loading_history_does_not_touch_shared_budget(backend, tmp_path):
    db = str(tmp_path / "audit.db")
    worker = WarderBox(budget=5.0, state_backend=backend, db_path=db)

    @worker.tool(cost=1.0)
    def tool():
        return "x"

    tool()
    dashboard = WarderBox(budget=5.0, state_backend=backend, db_path=db)
    assert dashboard.load_history_from_db() == 1
    assert dashboard.load_history_from_db() == 1
    assert worker.budget_summary["spent"] == 1.0


class _Down:
    """Every call fails, as a backend does during an outage."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise StateBackendError("connection refused")
        return fail


def test_outage_blocks_new_calls_instead_of_running_unmetered():
    shield = WarderBox(budget=5.0, state_backend=_Down())
    events = []
    shield.notifier.on("state_backend_error", lambda e, d: events.append(d))
    ran = []

    @shield.tool(cost=0.1)
    def tool():
        ran.append(1)

    result = tool()
    assert result.blocked and "State backend unavailable" in result.block_reason
    assert not ran and events


class _Flaky:
    """Delegates, except for commits that fail while `down` is set."""

    def __init__(self, inner):
        self.inner = inner
        self.down = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def budget_commit(self, *args):
        if self.down:
            raise StateBackendError("connection lost")
        return self.inner.budget_commit(*args)


def test_charge_lost_during_outage_is_kept_and_retried(tmp_path):
    flaky = _Flaky(SQLiteStateBackend(tmp_path / "s.db"))
    errors = []
    tracker = BudgetTracker(budget=1.0, backend=flaky)
    tracker.on_backend_error = errors.append
    reservation = tracker.reserve("1", "t", 0.6)
    flaky.down = True
    tracker.commit(reservation.reservation_id)
    assert errors
    assert tracker.spent == 0.6  # still counted locally
    assert tracker.summary()["pending_charges"] == 1
    flaky.down = False
    assert tracker.reserve("2", "t", 0.6) is None  # spend survived the outage
    assert BudgetTracker(budget=1.0, backend=flaky).spent == 0.6
    assert tracker.summary()["pending_charges"] == 0


def _budget_worker(path, calls, queue):
    backend = SQLiteStateBackend(path)
    shield = WarderBox(
        budget=1.0, state_backend=backend, namespace="workers",
        global_rpm=25,
    )
    executed = []

    @shield.tool(cost=0.1)
    def tool():
        executed.append(1)
        time.sleep(0.001)

    for _ in range(calls):
        tool()
    queue.put(len(executed))
    backend.close()


@pytest.mark.parametrize("workers", [4])
def test_budget_holds_across_real_processes(tmp_path, workers):
    path = str(tmp_path / "state.db")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_budget_worker, args=(path, 20, queue))
        for _ in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)
    executed = sum(queue.get(timeout=5) for _ in procs)
    # 4 workers × 20 calls, $1 at $0.10: exactly 10 calls may run, not 40.
    assert executed == 10
    totals = SQLiteStateBackend(path).budget_totals("workers")
    assert round(totals[0], 10) == 1.0
    assert totals[2] == 0


def _rate_worker(path, queue):
    backend = SQLiteStateBackend(path)
    limiter = RateLimiter(global_rpm=25, backend=backend)
    queue.put(sum(limiter.reserve("t")[0] for _ in range(20)))


def test_rate_limit_holds_across_real_processes(tmp_path):
    path = str(tmp_path / "state.db")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_rate_worker, args=(path, queue)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert sum(queue.get(timeout=5) for _ in procs) == 25


def test_backend_survives_fork(tmp_path):
    backend = SQLiteStateBackend(tmp_path / "s.db")
    backend.budget_adjust("n", 1.0)
    ctx = multiprocessing.get_context("fork")

    def child():
        backend.budget_adjust("n", 1.0)

    p = ctx.Process(target=child)
    p.start()
    p.join(30)
    assert p.exitcode == 0
    assert backend.budget_totals("n")[0] == 2.0


def test_policy_configures_a_backend(tmp_path):
    state_path = tmp_path / "state.db"
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps({
        "budget": 1.0, "namespace": "prod",
        "state": {"backend": "sqlite", "path": str(state_path)},
    }))
    shield = load_policy(policy)

    @shield.tool(cost=0.5)
    def tool():
        return "x"

    tool()
    conn = sqlite3.connect(state_path)
    assert conn.execute("SELECT ns, spent FROM budget").fetchone() == ("prod", 0.5)


@pytest.mark.parametrize("state, message", [
    ({"backend": "memcached"}, "state.backend"),
    ({"backend": "sqlite"}, "state.path"),
    ("sqlite", "state must be"),
])
def test_policy_rejects_bad_state(tmp_path, state, message):
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps({"state": state}))
    assert any(message in issue for issue in validate_policy(policy))
    with pytest.raises(ValueError):
        load_policy(policy)


def test_backend_from_config_rejects_unknown():
    with pytest.raises(ValueError):
        backend_from_config({"backend": "nope"})


def test_redis_backend_needs_the_package_or_a_client(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_redis(name, *args, **kwargs):
        if name == "redis":
            raise ImportError("no redis")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_redis)
    with pytest.raises(ImportError, match=r"warderbox\[redis\]"):
        RedisStateBackend()


def test_outage_after_the_tool_ran_does_not_raise(tmp_path):
    flaky = _Flaky(SQLiteStateBackend(tmp_path / "s.db"))
    shield = WarderBox(budget=1.0, state_backend=flaky)
    errors = []
    shield.notifier.on("state_backend_error", lambda e, d: errors.append(d))

    def lost(*args):
        raise StateBackendError("connection lost")

    @shield.tool(cost=0.1)
    def tool():
        # The backend goes away while the tool is running.
        flaky.down = True
        flaky.budget_totals = lost
        return "done"

    result = tool()
    assert result.output == "done"
    assert errors
