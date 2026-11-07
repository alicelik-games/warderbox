"""What the audit chain detects in each mode, and what it does not."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3

import pytest

from warderbox import Action, ActionResult, WarderBox
from warderbox import logger as logger_module
from warderbox.logger import _KEYED_PREFIX, ActionLogger, _hash_payload


@pytest.fixture(autouse=True)
def _no_key_from_the_environment(monkeypatch):
    """These tests choose their keys; a WARDERBOX_AUDIT_KEY set in the shell
    running them must not change what is being tested."""
    monkeypatch.delenv("WARDERBOX_AUDIT_KEY", raising=False)


def _entry(tool="ping", action_id=None, **kwargs):
    action = Action(tool_name=tool, args={"a": 1})
    if action_id:
        action.id = action_id
    return ActionResult(action=action, output="ok", **kwargs)


def _filled(path, count=3, **kwargs):
    logger = ActionLogger(path, **kwargs)
    for _ in range(count):
        logger.log(_entry())
    return logger


def test_clean_log_verifies(tmp_path):
    logger = _filled(tmp_path / "a.db")
    report = logger.verify_report()
    assert report["ok"] and report["entries"] == 3


@pytest.mark.parametrize("statement", [
    "UPDATE actions SET output = 'x' WHERE seq = 2",
    "UPDATE actions SET blocked = 1 WHERE seq = 1",
    "UPDATE actions SET cost = 9 WHERE seq = 3",
    "DELETE FROM actions WHERE seq = 2",
    "DELETE FROM actions WHERE seq = 3",  # tail
    "DELETE FROM actions",  # everything
    "UPDATE actions SET entry_hash = NULL",  # nulled hashes
    "UPDATE actions SET entry_hash = NULL WHERE seq = 3",
    "UPDATE actions SET seq = 5 WHERE seq = 3",
])
def test_edits_and_deletions_are_detected(tmp_path, statement):
    logger = _filled(tmp_path / "a.db")
    logger._conn.execute(statement)
    logger._conn.commit()
    assert not logger.verify_integrity()


def test_truncation_stays_detected_after_more_writes(tmp_path):
    logger = _filled(tmp_path / "a.db")
    logger._conn.execute("DELETE FROM actions WHERE seq = 3")
    logger._conn.commit()
    logger.log(_entry())
    assert not logger.verify_integrity()


def _forge(path):
    """Rewrite every row and the chain state, as an attacker with DB access."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    prev = None
    fields = (
        "id", "tool_name", "args", "risk", "cost", "blocked", "block_reason",
        "error", "output", "snapshot_id", "fallback_tool", "reversible", "timestamp",
    )
    rows = conn.execute("SELECT * FROM actions ORDER BY seq").fetchall()
    for row in rows:
        payload = {name: row[name] for name in fields}
        payload["output"] = json.dumps("FORGED")
        payload["prev_hash"] = prev
        payload["seq"] = row["seq"]
        digest = _hash_payload(payload)
        conn.execute(
            "UPDATE actions SET output=?, prev_hash=?, entry_hash=? WHERE id=?",
            (payload["output"], prev, digest, row["id"]),
        )
        prev = digest
    conn.execute(
        "UPDATE audit_meta SET value=? WHERE key='chain'",
        (json.dumps({
            "count": len(rows), "head": prev, "keyed_since": None, "tampered": False,
        }),),
    )
    conn.execute("DELETE FROM audit_meta WHERE key='mac'")
    conn.commit()
    conn.close()


def test_unkeyed_chain_can_be_forged_and_docs_say_so(tmp_path):
    # This is a documented limit, kept as a test so the docs cannot drift.
    path = tmp_path / "a.db"
    _filled(path).close()
    _forge(path)
    assert ActionLogger(path).verify_integrity()
    assert "recompute" in logger_module.__doc__


def test_keyed_chain_rejects_full_recomputation(tmp_path):
    path = tmp_path / "a.db"
    _filled(path, audit_key="k").close()
    _forge(path)
    assert not ActionLogger(path, audit_key="k").verify_integrity()


def test_keyed_entries_are_hmacs_and_need_the_key(tmp_path):
    path = tmp_path / "a.db"
    logger = _filled(path, audit_key="k")
    assert logger.verify_integrity()
    row = logger.query()[0]
    assert row["entry_hash"].startswith(_KEYED_PREFIX)
    report = ActionLogger(path, audit_key="").verify_report()
    assert not report["ok"] and "key" in report["reason"]
    with pytest.raises(logger_module.AuditKeyError):
        ActionLogger(path, audit_key="wrong")


def test_key_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDERBOX_AUDIT_KEY", "from-env")
    logger = _filled(tmp_path / "a.db")
    assert logger.query()[0]["entry_hash"].startswith(_KEYED_PREFIX)
    assert logger.verify_integrity()


def test_keyed_chain_detects_state_table_tampering_even_after_writes(tmp_path):
    path = tmp_path / "a.db"
    logger = _filled(path, audit_key="k")
    logger._conn.execute("DELETE FROM actions WHERE seq = 3")
    chain = json.loads(logger._conn.execute(
        "SELECT value FROM audit_meta WHERE key='chain'"
    ).fetchone()[0])
    chain["count"] = 2
    logger._conn.execute(
        "UPDATE audit_meta SET value=? WHERE key='chain'", (json.dumps(chain),)
    )
    logger._conn.commit()
    assert not logger.verify_integrity()
    logger.log(_entry())  # must not launder the damage by re-signing
    assert not logger.verify_integrity()


def test_keyed_chain_detects_deleted_state_and_rows(tmp_path):
    path = tmp_path / "a.db"
    logger = _filled(path, audit_key="k")
    logger._conn.execute("DELETE FROM audit_meta")
    logger._conn.commit()
    logger.close()
    assert not ActionLogger(path, audit_key="k").verify_integrity()


def test_unkeyed_row_in_keyed_log_is_detected(tmp_path):
    path = tmp_path / "a.db"
    _filled(path, audit_key="k").close()
    keyless = ActionLogger(path, audit_key="")
    with pytest.raises(logger_module.AuditKeyError):
        keyless.log(_entry())
    assert ActionLogger(path, audit_key="k").verify_integrity()
    # A row slipped in some other way is still caught by verification.
    keyless._is_keyed_log = lambda *args: False
    keyless.log(_entry())
    keyless.close()
    assert not ActionLogger(path, audit_key="k").verify_integrity()


def test_moving_an_existing_log_to_keyed_mode_is_explicit(tmp_path):
    path = tmp_path / "a.db"
    _filled(path, audit_key="").close()
    logger = ActionLogger(path, audit_key="k")
    assert not logger.verify_integrity()
    logger.adopt_unkeyed_history()
    logger.log(_entry())
    assert logger.verify_integrity()
    assert logger.verify_report()["entries"] == 4


def test_shield_exposes_the_key_and_anchor_api(tmp_path):
    path = str(tmp_path / "a.db")
    _filled(path, audit_key="").close()
    shield = WarderBox(budget=1.0, db_path=path, audit_key="k")
    assert not shield.verify_audit()
    shield.adopt_unkeyed_audit_history()
    assert shield.verify_audit()
    anchor = shield.audit_head()
    assert anchor["count"] == 3
    assert shield.verify_audit(anchor)
    assert not shield.verify_audit({"count": 2, "head": "wrong"})
    with pytest.raises(ValueError):
        WarderBox().adopt_unkeyed_audit_history()


def test_anchor_detects_rollback_and_full_replacement(tmp_path):
    path = tmp_path / "a.db"
    logger = _filled(path)
    anchor = logger.audit_head()
    logger.log(_entry())
    assert logger.verify_integrity(expected_head=anchor)

    logger._conn.execute("DELETE FROM actions")
    logger._conn.execute("DELETE FROM audit_meta")
    logger._conn.commit()
    logger.close()
    fresh = ActionLogger(path)
    assert fresh.verify_integrity()  # looks like a new, empty log
    assert not fresh.verify_integrity(expected_head=anchor)


def test_duplicate_ids_are_all_kept(tmp_path):
    logger = ActionLogger(tmp_path / "a.db")
    logger.log(_entry(tool="harmless", action_id="toolu_1"))
    logger.log(ActionResult(
        action=Action(tool_name="query_db", args={"sql": "DROP TABLE x"}, id="toolu_1"),
        blocked=True, block_reason="CRITICAL",
    ))
    rows = logger.query()
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"toolu_1", "toolu_1#dup1"}
    assert logger.query(blocked_only=True)[0]["tool_name"] == "query_db"
    assert logger.verify_integrity()


def test_rows_from_older_versions_still_verify(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE actions (id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
        args TEXT, risk TEXT, cost REAL DEFAULT 0, blocked INTEGER DEFAULT 0,
        block_reason TEXT, error TEXT, output TEXT, snapshot_id TEXT,
        fallback_tool TEXT, reversible INTEGER, prev_hash TEXT, entry_hash TEXT,
        timestamp REAL NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO actions (id, tool_name, timestamp) VALUES ('unhashed', 't', 1.0)"
    )
    payload = {
        "id": "hashed", "tool_name": "t", "args": "{}", "risk": "low", "cost": 0.0,
        "blocked": 0, "block_reason": None, "error": None, "output": None,
        "snapshot_id": None, "fallback_tool": None, "reversible": None,
        "timestamp": 2.0, "prev_hash": None,
    }
    conn.execute(
        """INSERT INTO actions (id, tool_name, args, risk, cost, blocked, timestamp,
        entry_hash) VALUES ('hashed', 't', '{}', 'low', 0.0, 0, 2.0, ?)""",
        (_hash_payload(payload),),
    )
    conn.commit()
    conn.close()

    logger = ActionLogger(path)
    assert logger.verify_integrity()
    logger.log(_entry())
    assert logger.verify_integrity()
    assert logger.verify_report()["entries"] == 2


def _writer(path, count):
    logger = ActionLogger(path)
    for _ in range(count):
        logger.log(_entry())
    logger.close()


def test_writers_in_other_processes_do_not_fork_the_chain(tmp_path):
    path = str(tmp_path / "a.db")
    ctx = multiprocessing.get_context("fork")
    workers = [ctx.Process(target=_writer, args=(path, 40)) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(60)
    assert all(w.exitcode == 0 for w in workers)
    report = ActionLogger(path).verify_report()
    assert report["ok"], report
    assert report["entries"] == 160


def test_log_failure_is_reported_not_silent(tmp_path):
    shield = WarderBox(budget=1.0, db_path=str(tmp_path / "a.db"))
    events = []
    shield.notifier.on("audit_log_error", lambda e, d: events.append(d))

    @shield.tool(cost=0.01)
    def ping():
        return "pong"

    def broken(_result):
        raise sqlite3.OperationalError("disk full")

    shield._logger.log = broken
    assert ping().output == "pong"
    assert events and "disk full" in events[0]["error"]
    assert shield.stats()["audit_log_errors"] == 1



def _keyed_log(path, key="right", count=2):
    shield = WarderBox(db_path=str(path), audit_key=key)

    @shield.tool(cost=0.0)
    def ping(x):
        return x

    for i in range(count):
        ping(i)
    return shield


@pytest.mark.parametrize("count", [0, 2])
def test_wrong_key_is_refused_and_leaves_the_log_intact(tmp_path, count):
    db = tmp_path / "a.db"
    _keyed_log(db, count=count)
    before = sqlite3.connect(db).execute("SELECT * FROM audit_meta ORDER BY key").fetchall()
    with pytest.raises(logger_module.AuditKeyError):
        WarderBox(db_path=str(db), audit_key="wrong")
    after = sqlite3.connect(db).execute("SELECT * FROM audit_meta ORDER BY key").fetchall()
    assert before == after
    assert WarderBox(db_path=str(db), audit_key="right").verify_audit()


def test_wrong_key_error_is_a_value_error(tmp_path):
    db = tmp_path / "a.db"
    _keyed_log(db)
    with pytest.raises(ValueError, match="does not match"):
        ActionLogger(db, audit_key="wrong")


def test_right_key_still_flags_altered_chain_state(tmp_path):
    db = tmp_path / "a.db"
    _keyed_log(db)
    conn = sqlite3.connect(db)
    chain = json.loads(conn.execute("SELECT value FROM audit_meta WHERE key='chain'").fetchone()[0])
    chain["count"] = 1
    conn.execute("UPDATE audit_meta SET value=? WHERE key='chain'", (json.dumps(chain),))
    conn.commit()
    conn.close()
    assert not WarderBox(db_path=str(db), audit_key="right").verify_audit()
    # Sticky: a later write cannot launder it.
    _keyed_log(db, count=1)
    assert not WarderBox(db_path=str(db), audit_key="right").verify_audit()


def test_altered_key_record_is_refused_loudly(tmp_path):
    db = tmp_path / "a.db"
    _keyed_log(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_meta SET value='0' WHERE key='key_check'")
    conn.commit()
    conn.close()
    with pytest.raises(logger_module.AuditKeyError):
        WarderBox(db_path=str(db), audit_key="right")


def test_deleted_key_record_is_recovered_from_the_entries(tmp_path):
    db = tmp_path / "a.db"
    _keyed_log(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM audit_meta WHERE key='key_check'")
    conn.commit()
    conn.close()
    with pytest.raises(logger_module.AuditKeyError):
        WarderBox(db_path=str(db), audit_key="wrong")
    assert WarderBox(db_path=str(db), audit_key="right").verify_audit()


def test_keyless_process_can_read_but_not_write_a_keyed_log(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDERBOX_AUDIT_KEY", raising=False)
    db = tmp_path / "a.db"
    _keyed_log(db)
    reader = WarderBox(db_path=str(db))
    assert reader.load_history_from_db() == 2
    errors = []
    reader.notifier.on("audit_log_error", lambda e, d: errors.append(d))

    @reader.tool(cost=0.0)
    def ping(x):
        return x

    assert ping(9).output == 9
    assert errors and "keyed" in errors[0]["error"]
    assert reader.stats()["audit_log_errors"] == 1
    assert WarderBox(db_path=str(db), audit_key="right").verify_audit()


def test_unkeyed_log_can_still_be_written_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDERBOX_AUDIT_KEY", raising=False)
    db = tmp_path / "a.db"
    shield = WarderBox(db_path=str(db))

    @shield.tool(cost=0.0)
    def ping(x):
        return x

    ping(1)
    reopened = WarderBox(db_path=str(db))
    assert reopened.verify_audit()
    assert reopened.stats()["audit_log_errors"] == 0


def test_policy_audit_key_env(tmp_path, monkeypatch):
    db = str(tmp_path / "a.db")
    monkeypatch.delenv("MY_AUDIT_KEY", raising=False)
    with pytest.raises(ValueError, match="MY_AUDIT_KEY"):
        WarderBox.from_config({"db_path": db, "audit_key_env": "MY_AUDIT_KEY"})
    monkeypatch.setenv("MY_AUDIT_KEY", "")
    with pytest.raises(ValueError, match="MY_AUDIT_KEY"):
        WarderBox.from_config({"db_path": db, "audit_key_env": "MY_AUDIT_KEY"})
    monkeypatch.setenv("MY_AUDIT_KEY", "k1")
    shield = WarderBox.from_config({"db_path": db, "audit_key_env": "MY_AUDIT_KEY"})
    assert shield._logger._key == b"k1"


@pytest.mark.parametrize("value", ["false", "true", 1, 0, None])
def test_policy_require_audit_key_must_be_boolean(value):
    with pytest.raises(ValueError, match="true or false"):
        WarderBox.from_config({"require_audit_key": value})
