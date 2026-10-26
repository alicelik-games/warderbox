"""Persistent action logger backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from warderbox.action import ActionResult

_SCHEMA = """
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
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_tool ON actions(tool_name);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(timestamp);
CREATE INDEX IF NOT EXISTS idx_actions_blocked ON actions(blocked);
"""


class ActionLogger:
    """Logs every action to a SQLite database for audit and analysis."""

    def __init__(self, db_path: str | Path = "warderbox.db", masker=None) -> None:
        self._path = Path(db_path)
        # Agent frameworks call tools from worker threads; the logger must be
        # usable across threads. We serialize access with our own lock.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            # Migrate databases created before fallback_tool existed.
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(actions)")
            }
            if "fallback_tool" not in columns:
                self._conn.execute(
                    "ALTER TABLE actions ADD COLUMN fallback_tool TEXT"
                )
            if "reversible" not in columns:
                self._conn.execute(
                    "ALTER TABLE actions ADD COLUMN reversible INTEGER"
                )
            self._conn.commit()
        self._masker = masker

    def log(self, result: ActionResult) -> None:
        args = result.action.args
        output = result.output
        error = result.error
        block_reason = result.block_reason
        if self._masker:
            args = self._masker.mask(args)
            if output is not None:
                output = self._masker.mask(output) if isinstance(
                    output, (str, dict, list)
                ) else self._masker.mask_string(str(output))
            if error is not None:
                error = self._masker.mask_string(error)
            if block_reason is not None:
                block_reason = self._masker.mask_string(block_reason)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO actions
                   (id, tool_name, args, risk, cost, blocked, block_reason, error, output, snapshot_id, fallback_tool, reversible, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.action.id,
                    result.action.tool_name,
                    json.dumps(args, default=str),
                    result.action.risk.value,
                    result.cost,
                    int(result.blocked),
                    block_reason,
                    error,
                    json.dumps(output, default=str) if output is not None else None,
                    result.snapshot_id,
                    result.fallback_tool,
                    (
                        int(result.reversible)
                        if result.reversible is not None else None
                    ),
                    result.action.timestamp,
                ),
            )
            self._conn.commit()

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
