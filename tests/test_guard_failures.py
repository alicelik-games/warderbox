"""A failing guard, an outage or a failed fallback still gives a clean result."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from warderbox import CircuitBreaker, WarderBox
from warderbox.dashboard import _DashboardHandler
from warderbox.fallback import FallbackManager
from warderbox.idempotency import IdempotencyGuard, MemoryBackend
from warderbox.masking import DataMasker
from warderbox.metrics import prometheus_metrics
from warderbox.ratelimit import ToolLimit
from warderbox.state import StateBackendError
from warderbox.timeout import TimeoutExecutor, TimeoutPolicy


class _Down:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise StateBackendError("connection refused")
        return fail


def _with_fallback(**kwargs):
    shield = WarderBox(budget=5.0, **kwargs)
    fallback = FallbackManager()
    ran = []
    fallback.register(
        "primary", "backup", fallback_fn=lambda *a, **k: ran.append(1), fallback_cost=0.1
    )
    shield.set_fallback(fallback)
    breaker = CircuitBreaker(failure_threshold=1)
    shield.set_circuit_breaker(breaker)
    breaker.record_failure("primary")

    @shield.tool(cost=0.1)
    def primary():
        return "real"

    return shield, primary, ran


def test_blocked_fallback_keeps_its_attribution(tmp_path):
    db = str(tmp_path / "a.db")
    shield, primary, ran = _with_fallback(db_path=db)
    shield._rate_limiter.__init__(tool_limits={"backup": ToolLimit(rpm=0)})
    events = []
    shield.notifier.on("rate_limited", lambda e, d: events.append(d))
    shield.notifier.on("action_blocked", lambda e, d: events.append(d))
    result = primary()
    assert result.blocked
    assert result.fallback_tool == "backup"
    assert shield.history()[-1]["fallback"] == "backup"
    assert not ran
    assert events and events[-1]["fallback"] == "backup"
    restored = WarderBox()
    restored.load_history_from_db(db)
    assert restored.history()[-1]["fallback"] == "backup"


def test_blocked_primary_has_no_fallback_attribution():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.1)
    def query_db(sql):
        return "ran"

    result = query_db(sql="DROP TABLE users")
    assert result.blocked and result.fallback_tool is None


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_thread_timeout_reraises_base_exceptions(error):
    def tool():
        raise error("stop")

    with pytest.raises(error):
        TimeoutExecutor(TimeoutPolicy(default_seconds=1)).execute("t", tool)


def test_idempotency_backend_failure_blocks_without_raising():
    class Broken(MemoryBackend):
        def set_if_absent(self, *args, **kwargs):
            raise RuntimeError("backend down")

        def get(self, *args, **kwargs):
            raise RuntimeError("backend down")

    guard = IdempotencyGuard(backend=Broken())
    guard.add_protected_tool("charge")
    shield = WarderBox(budget=5.0)
    shield.set_idempotency(guard)
    events, ran = [], []
    shield.notifier.on("guard_error", lambda e, d: events.append(d))

    @shield.tool(cost=0.1)
    def charge():
        ran.append(1)

    result = charge()
    assert result.blocked and "Idempotency backend failed" in result.block_reason
    assert not ran and events
    assert shield.budget_summary["reserved"] == 0


def test_snapshot_failure_blocks_without_raising():
    shield = WarderBox(budget=5.0)
    ran = []

    def capture():
        raise RuntimeError("cannot capture")

    @shield.tool(cost=0.1, snapshot_state=capture, restore_state=lambda s: None)
    def write():
        ran.append(1)

    result = write()
    assert result.blocked and "Snapshot failed" in result.block_reason
    assert not ran
    assert shield.budget_summary["reserved"] == 0


def test_stats_survive_an_outage_and_say_so():
    shield = WarderBox(budget=5.0, state_backend=_Down())
    stats = shield.stats()
    assert stats["degraded"] is True
    assert stats["budget"]["degraded"] is True
    assert stats["rate_limits"]["degraded"] is True
    assert "warderbox_degraded 1" in prometheus_metrics(shield)


def test_stats_are_not_degraded_normally():
    shield = WarderBox(budget=5.0)
    assert shield.stats()["degraded"] is False
    assert "warderbox_degraded 0" in prometheus_metrics(shield)


@pytest.fixture
def dashboard():
    from http.server import ThreadingHTTPServer

    shield = WarderBox(budget=5.0, state_backend=_Down())

    class Handler(_DashboardHandler):
        pass

    Handler.shield = shield
    Handler.router = None
    Handler.estimator = None
    Handler.anomaly_detector = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", shield
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    "path", ["/api/stats", "/api/history", "/api/finops", "/api/routing", "/api/estimator", "/api/alerts"]
)
def test_dashboard_answers_during_an_outage(dashboard, path):
    url, _shield = dashboard
    with urllib.request.urlopen(url + path, timeout=5) as response:
        assert response.status == 200
        json.loads(response.read())


def test_dashboard_reports_a_failing_panel_as_json(dashboard, monkeypatch):
    url, shield = dashboard
    monkeypatch.setattr(shield, "stats", lambda: 1 / 0)
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(url + "/api/stats", timeout=5)
    assert caught.value.code == 503
    assert json.loads(caught.value.read())["degraded"] is True


def test_history_masks_errors_and_block_reasons_unless_asked():
    shield = WarderBox(budget=5.0)

    @shield.tool(cost=0.0)
    def tool():
        raise ValueError("failed for ali@example.com")

    tool()
    assert "ali@example.com" not in shield.history()[-1]["error"]
    assert "ali@example.com" in shield.history(raw=True)[-1]["error"]


def test_history_respects_a_disabled_masker():
    shield = WarderBox(budget=5.0)
    shield.set_masker(DataMasker(enabled=False))

    @shield.tool(cost=0.0)
    def tool():
        raise ValueError("failed for ali@example.com")

    tool()
    assert "ali@example.com" in shield.history()[-1]["error"]


def test_require_audit_key(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDERBOX_AUDIT_KEY", raising=False)
    db = str(tmp_path / "a.db")
    with pytest.raises(ValueError, match="audit_key"):
        WarderBox(db_path=db, require_audit_key=True)
    assert WarderBox(db_path=db, require_audit_key=True, audit_key="k").verify_audit()
    monkeypatch.setenv("WARDERBOX_AUDIT_KEY", "k")
    WarderBox(db_path=db, require_audit_key=True)
    assert WarderBox.from_config({"require_audit_key": True}) is not None
    monkeypatch.delenv("WARDERBOX_AUDIT_KEY")
    with pytest.raises(ValueError):
        WarderBox.from_config({"require_audit_key": True})


def test_require_audit_key_is_explicit_and_backwards_compatible(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDERBOX_AUDIT_KEY", raising=False)
    db = str(tmp_path / "prod.db")
    # Existing keyless code keeps working on upgrade.
    WarderBox(db_path=db)
    # The stronger mode is opt-in.
    with pytest.raises(ValueError, match="audit_key"):
        WarderBox(db_path=db, require_audit_key=True)
    # A keyed production log starts normally.
    WarderBox(db_path=db, audit_key="prod-key", require_audit_key=True)


def test_agent_pool_passes_audit_key_and_policy_can_supply_one(tmp_path, monkeypatch):
    from warderbox import AgentPool

    db = str(tmp_path / "pool.db")
    pool = AgentPool(global_db_path=db, audit_key="pool-key")
    shield = pool.register("agent")
    assert shield._logger is not None
    assert shield._logger._key == b"pool-key"

    policy = tmp_path / "policy.json"
    policy.write_text('{"audit_key": "policy-key", "require_audit_key": true}')
    from warderbox import load_policy
    loaded = load_policy(policy)
    assert loaded._logger is None  # no db_path, key not used
    policy.write_text(
        '{"db_path": "' + str(tmp_path / "policy.db") + '", '
        '"audit_key": "policy-key", "require_audit_key": true}'
    )
    loaded = load_policy(policy)
    assert loaded._logger is not None
    assert loaded._logger._key == b"policy-key"
