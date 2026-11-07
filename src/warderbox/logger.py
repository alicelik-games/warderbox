"""Persistent action logger backed by SQLite.

The audit log is a hash chain: every entry commits to the previous entry, a
sequence number and its own content. What that proves depends on the mode:

* **Without a key** the chain is tamper-evident against naive edits only. It
  detects changed, reordered, removed and truncated rows, but anyone who can
  write to the database file can recompute the whole chain and pass
  verification.
* **With a key** (``audit_key=`` or ``WARDERBOX_AUDIT_KEY``) entries are HMACs.
  Someone with database access but not the key cannot forge entries. They can
  still restore an old copy of the file or delete it entirely, which is why
  the key alone does not prove completeness.
* **With an external anchor** (save :meth:`ActionLogger.audit_head` somewhere
  the agent cannot write, then pass it to ``verify_integrity(expected_head=)``)
  rollback and deletion back past the anchor are detected too. Entries written
  after the last anchor can still be lost in unkeyed mode.

Opening a keyed log with a different key raises :class:`AuditKeyError` before
anything is written, so a mistyped key cannot mark a healthy log as damaged.
Without a key a keyed log can still be read, but not written to.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from warderbox.action import ActionResult
from warderbox.masking import DataMasker

_STATEMENTS = (
    """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    args TEXT,
    risk TEXT,
    cost REAL DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    block_reason TEXT,
    error TEXT,
    output TEXT,
    snapshot_id TEXT,
    fallback_tool TEXT,
    reversible INTEGER,
    prev_hash TEXT,
    entry_hash TEXT,
    seq INTEGER,
    timestamp REAL NOT NULL
)
""",
    "CREATE TABLE IF NOT EXISTS audit_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_actions_tool ON actions(tool_name)",
    "CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_actions_blocked ON actions(blocked)",
)

_SCHEMA = ";\n".join(_STATEMENTS)

_ADDED_COLUMNS = {
    "fallback_tool": "TEXT",
    "reversible": "INTEGER",
    "prev_hash": "TEXT",
    "entry_hash": "TEXT",
    "seq": "INTEGER",
}

_KEYED_PREFIX = "hmac:"
_HASHED_FIELDS = (
    "id", "tool_name", "args", "risk", "cost", "blocked", "block_reason",
    "error", "output", "snapshot_id", "fallback_tool", "reversible", "timestamp",
)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


_KEY_CHECK_LABEL = b"warderbox-audit-key-check-v1"


class AuditKeyError(ValueError):
    """The audit key does not fit this log: nothing was written to it."""


def _as_key(key: str | bytes | None) -> bytes | None:
    if key is None or key == "" or key == b"":
        return None
    return key.encode("utf-8") if isinstance(key, str) else key


class ActionLogger:
    """Logs every action to a SQLite database for audit and analysis."""

    def __init__(
        self,
        db_path: str | Path = "warderbox.db",
        masker=None,
        audit_key: str | bytes | None = None,
    ) -> None:
        self._path = Path(db_path)
        self._key = _as_key(
            audit_key if audit_key is not None
            else os.environ.get("WARDERBOX_AUDIT_KEY")
        )
        # Agent frameworks call tools from worker threads; the logger must be
        # usable across threads. We serialize access with our own lock.
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._initialize()
        # Mask PII/secrets by default. Pass
        # ``masker=DataMasker(enabled=False)`` to opt out explicitly.
        self._masker = masker if masker is not None else DataMasker()

    def _initialize(self) -> None:
        self._conn.execute("PRAGMA busy_timeout=30000")
        for attempt in range(50):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:
                # Another process is creating the same database.
                if attempt == 49:
                    raise
                time.sleep(0.1)
        try:
            for statement in _STATEMENTS:
                self._conn.execute(statement)
            # Migrate databases created by older versions.
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(actions)")
            }
            for name, kind in _ADDED_COLUMNS.items():
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE actions ADD COLUMN {name} {kind}"
                    )
            self._load_chain()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    # -- Chain state --

    def _mac(self, chain: dict[str, Any]) -> str:
        return hmac.new(self._key, _canonical(chain), hashlib.sha256).hexdigest()

    def _key_check(self) -> str:
        return hmac.new(self._key, _KEY_CHECK_LABEL, hashlib.sha256).hexdigest()

    def _save_chain(self, chain: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO audit_meta (key, value) VALUES ('chain', ?)",
            (json.dumps(chain, sort_keys=True),),
        )
        if self._key:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_meta (key, value) VALUES ('mac', ?)",
                (self._mac(chain),),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_meta (key, value) VALUES ('key_check', ?)",
                (self._key_check(),),
            )

    def _stored_key_check(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM audit_meta WHERE key = 'key_check'"
        ).fetchone()
        return row[0] if row else None

    def _last_keyed_row_matches(self) -> bool | None:
        """Whether our key produced the newest keyed entry; None if there is none."""
        row = self._conn.execute(
            "SELECT * FROM actions WHERE entry_hash LIKE ? "
            "ORDER BY COALESCE(seq, -1) DESC, rowid DESC LIMIT 1",
            (_KEYED_PREFIX + "%",),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        payload = {name: data[name] for name in _HASHED_FIELDS}
        payload["prev_hash"] = data["prev_hash"]
        if data["seq"] is not None:
            payload["seq"] = data["seq"]
        return hmac.compare_digest(data["entry_hash"], self._digest(payload, keyed=True))

    def _confirm_key(self, mac: str | None) -> None:
        """Refuse a key that is not this log's before anything is written.

        Without this, a mistyped key would see a signature mismatch, take it for
        tampering and mark a healthy log as damaged for good.
        """
        check = self._stored_key_check()
        if check is not None:
            if hmac.compare_digest(check, self._key_check()):
                return
        else:
            matches = self._last_keyed_row_matches()
            if matches or (matches is None and mac is None):
                return
        raise AuditKeyError(
            f"The audit key does not match the log at {self._path} (or the log's "
            f"key record was altered). Nothing was written; check audit_key / "
            f"WARDERBOX_AUDIT_KEY."
        )

    def _is_keyed_log(self, chain: dict[str, Any], mac: str | None) -> bool:
        return (
            mac is not None
            or chain.get("keyed_since") is not None
            or self._stored_key_check() is not None
        )

    def _read_meta(self) -> tuple[dict[str, Any] | None, str | None]:
        rows = dict(self._conn.execute(
            "SELECT key, value FROM audit_meta WHERE key IN ('chain', 'mac')"
        ).fetchall() or [])
        raw = rows.get("chain")
        if raw is None:
            return None, rows.get("mac")
        try:
            return json.loads(raw), rows.get("mac")
        except ValueError:
            return None, rows.get("mac")

    def _chained_rows(self) -> tuple[int, str | None, bool]:
        count, last, keyed = 0, None, False
        for row in self._conn.execute(
            "SELECT entry_hash FROM actions WHERE entry_hash IS NOT NULL "
            "ORDER BY COALESCE(seq, -1), rowid"
        ):
            count += 1
            last = row[0]
            keyed = keyed or last.startswith(_KEYED_PREFIX)
        return count, last, keyed

    def _load_chain(self) -> dict[str, Any]:
        """Read the chain head, flagging anything that proves tampering.

        The flag is sticky: it is written back so that the next ``log()`` call
        cannot launder a damaged head by re-signing it. Must run in a write
        transaction.
        """
        chain, mac = self._read_meta()
        if self._key:
            self._confirm_key(mac)
        if chain is None:
            count, last, keyed = self._chained_rows()
            chain = {
                "count": count,
                "head": last,
                "keyed_since": 1 if self._key and count == 0 else None,
                # Chain state only disappears from a keyed log by tampering.
                # For unkeyed rows this is ordinary migration from old versions.
                "tampered": keyed,
            }
            self._save_chain(chain)
            return chain
        if self._key:
            if mac is None:
                # Unkeyed until now. An empty log can start keyed immediately;
                # existing rows need an explicit adopt_unkeyed_history().
                if chain["count"] == 0 and chain.get("keyed_since") is None:
                    chain["keyed_since"] = 1
                    self._save_chain(chain)
            elif not hmac.compare_digest(mac, self._mac(chain)):
                chain["tampered"] = True
                self._save_chain(chain)
        return chain

    def adopt_unkeyed_history(self) -> None:
        """Accept rows written before a key was configured.

        Call this once, after checking the existing log, when moving a database
        to keyed mode. Those older rows stay only as trustworthy as they were.
        Later rows must be keyed. Requires a key.
        """
        if not self._key:
            raise ValueError("adopt_unkeyed_history() requires an audit key")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                chain = self._load_chain()
                if chain.get("keyed_since") is None:
                    chain["keyed_since"] = chain["count"] + 1
                    self._save_chain(chain)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def audit_head(self) -> dict[str, Any]:
        """The current chain position, to store outside the agent's reach."""
        with self._lock:
            chain, _mac = self._read_meta()
        chain = chain or {"count": 0, "head": None}
        return {"count": chain["count"], "head": chain["head"]}

    def _digest(self, payload: dict[str, Any], keyed: bool) -> str:
        if keyed:
            digest = hmac.new(self._key, _canonical(payload), hashlib.sha256)
            return _KEYED_PREFIX + digest.hexdigest()
        return _hash_payload(payload)

    # -- Writing --

    def _unique_id(self, action_id: str) -> str:
        """Audit entries are never dropped, even when a caller reuses an id
        (for example a provider's ``tool_use`` id replayed by an agent)."""
        candidate = action_id
        suffix = 1
        while self._conn.execute(
            "SELECT 1 FROM actions WHERE id = ?", (candidate,)
        ).fetchone():
            candidate = f"{action_id}#dup{suffix}"
            suffix += 1
        return candidate

    def log(self, result: ActionResult) -> None:
        args = result.action.args
        output = result.output
        error = result.error
        block_reason = result.block_reason
        if self._masker:
            args = self._masker.mask(args)
            if output is not None:
                output = self._masker.mask(output)
            if error is not None:
                error = self._masker.mask_string(error)
            if block_reason is not None:
                block_reason = self._masker.mask_string(block_reason)
        args_json = json.dumps(args, default=str)
        output_json = json.dumps(output, default=str) if output is not None else None
        reversible = int(result.reversible) if result.reversible is not None else None
        with self._lock:
            # The write lock covers read-head + insert + write-head, so writers
            # in other processes cannot fork the chain.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                chain = self._load_chain()
                if not self._key and self._is_keyed_log(chain, self._read_meta()[1]):
                    # An unkeyed entry would break a keyed chain for good.
                    raise AuditKeyError(
                        f"The audit log at {self._path} is keyed: pass audit_key= "
                        f"or set WARDERBOX_AUDIT_KEY to write to it."
                    )
                seq = chain["count"] + 1
                prev_hash = chain["head"]
                entry = {
                    "id": self._unique_id(result.action.id),
                    "tool_name": result.action.tool_name,
                    "args": args_json,
                    "risk": result.action.risk.value,
                    "cost": result.cost,
                    "blocked": int(result.blocked),
                    "block_reason": block_reason,
                    "error": error,
                    "output": output_json,
                    "snapshot_id": result.snapshot_id,
                    "fallback_tool": result.fallback_tool,
                    "reversible": reversible,
                    "timestamp": result.action.timestamp,
                }
                entry_hash = self._digest(
                    {**entry, "prev_hash": prev_hash, "seq": seq},
                    keyed=self._key is not None,
                )
                # A direct SQL UPDATE still changes the row, but the verifier
                # detects it.
                self._conn.execute(
                    """INSERT INTO actions
                       (id, tool_name, args, risk, cost, blocked, block_reason, error, output, snapshot_id, fallback_tool, reversible, prev_hash, entry_hash, seq, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry["id"],
                        entry["tool_name"],
                        args_json,
                        entry["risk"],
                        entry["cost"],
                        entry["blocked"],
                        block_reason,
                        error,
                        output_json,
                        entry["snapshot_id"],
                        entry["fallback_tool"],
                        reversible,
                        prev_hash,
                        entry_hash,
                        seq,
                        entry["timestamp"],
                    ),
                )
                chain["count"] = seq
                chain["head"] = entry_hash
                self._save_chain(chain)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(
        self,
        tool_name: str | None = None,
        blocked_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        conditions = []
        params: list[Any] = []

        if tool_name:
            conditions.append("tool_name = ?")
            params.append(tool_name)
        if blocked_only:
            conditions.append("blocked = 1")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM actions {where} ORDER BY timestamp DESC LIMIT ?",
                params + [limit],
            ).fetchall()

        return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT
                    COUNT(*) as total,
                    COALESCE(SUM(blocked), 0) as blocked,
                    COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), 0) as errors,
                    COALESCE(SUM(cost), 0.0) as total_cost,
                    COUNT(DISTINCT COALESCE(fallback_tool, tool_name)) as unique_tools
                   FROM actions"""
            ).fetchone()
            result = dict(row)
            by_tool_rows = self._conn.execute(
                """SELECT COALESCE(fallback_tool, tool_name) as tool_name,
                       COUNT(*) as calls,
                       COALESCE(SUM(cost), 0.0) as cost
                   FROM actions
                   GROUP BY COALESCE(fallback_tool, tool_name)
                   ORDER BY calls DESC
                   LIMIT 10"""
            ).fetchall()
        result["by_tool"] = {
            r["tool_name"]: {"calls": r["calls"], "cost": round(r["cost"], 4)}
            for r in by_tool_rows
        }
        return result

    def verify_integrity(self, expected_head: dict[str, Any] | None = None) -> bool:
        """Verify the hash chain. See the module docstring for what it proves.

        ``expected_head`` is a value previously returned by :meth:`audit_head`.
        """
        return self.verify_report(expected_head)["ok"]

    def verify_report(self, expected_head: dict[str, Any] | None = None) -> dict[str, Any]:
        """Like :meth:`verify_integrity`, but says why verification failed."""
        with self._lock:
            # One read transaction gives a consistent view under WAL.
            self._conn.execute("BEGIN")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM actions ORDER BY COALESCE(seq, -1), rowid"
                ).fetchall()
                chain, mac = self._read_meta()
            finally:
                self._conn.rollback()
        reason, verified = self._verify_rows(rows, chain, mac, expected_head)
        return {
            "ok": reason is None,
            "reason": reason,
            "entries": verified,
            "keyed": self._key is not None,
        }

    def _verify_rows(
        self,
        rows: list[sqlite3.Row],
        chain: dict[str, Any] | None,
        mac: str | None,
        expected_head: dict[str, Any] | None,
    ) -> tuple[str | None, int]:
        keyed_since = None
        if chain is not None and self._key:
            if mac is not None and hmac.compare_digest(mac, self._mac(chain)):
                keyed_since = chain.get("keyed_since")
            elif mac is not None:
                return "chain state signature is invalid", 0

        position = 0
        prev_hash = None
        anchored = False
        for row in rows:
            data = dict(row)
            entry_hash = data["entry_hash"]
            if entry_hash is None:
                # Only rows from before the hash chain existed may be unhashed.
                if position or data["seq"] is not None:
                    return f"entry '{data['id']}' has no hash", position
                continue
            position += 1
            keyed = entry_hash.startswith(_KEYED_PREFIX)
            if keyed and not self._key:
                return "log is keyed; an audit key is required to verify it", position
            if self._key:
                if keyed_since is None:
                    return (
                        "log has entries not covered by the audit key; call "
                        "adopt_unkeyed_history() if they are expected"
                    ), position
                if position >= keyed_since and not keyed:
                    return f"unkeyed entry at position {position}", position
            payload = {name: data[name] for name in _HASHED_FIELDS}
            payload["prev_hash"] = prev_hash
            if data["seq"] is not None:
                if data["seq"] != position:
                    return f"sequence gap at position {position}", position
                payload["seq"] = data["seq"]
            expected = self._digest(payload, keyed=keyed)
            if (
                not hmac.compare_digest(entry_hash, expected)
                or data["prev_hash"] != prev_hash
            ):
                return f"entry at position {position} does not match", position
            prev_hash = entry_hash
            if expected_head and position == expected_head.get("count"):
                if not hmac.compare_digest(entry_hash, expected_head.get("head") or ""):
                    return "anchored head does not match", position
                anchored = True

        if chain is None:
            return "chain state is missing", position
        if chain.get("tampered"):
            return "chain state was found damaged earlier", position
        if chain["count"] != position:
            return (
                f"chain records {chain['count']} entries, found {position}"
            ), position
        if chain["head"] != prev_hash:
            return "last entry does not match the chain head", position
        if expected_head and expected_head.get("count") and not anchored:
            return "anchored entry is missing", position
        return None, position

    def export_json(self, path: str | Path) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions ORDER BY timestamp"
            ).fetchall()
        data = [dict(r) for r in rows]
        Path(path).write_text(json.dumps(data, indent=2, default=str))
        return len(data)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
