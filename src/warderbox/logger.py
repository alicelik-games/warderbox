"""Persistent action logger backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
import time
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
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._masker = masker

    def log(self, result: ActionResult) -> None:
        args = result.action.args
        if self._masker:
            args = self._masker.mask(args)
        self._conn.execute(
            """INSERT OR REPLACE INTO actions
               (id, tool_name, args, risk, cost, blocked, block_reason, error, output, snapshot_id, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.action.id,
                result.action.tool_name,
                json.dumps(args, default=str),
                result.action.risk.value,
                result.cost,
                int(result.blocked),
                result.block_reason,
                result.error,
                json.dumps(result.output, default=str) if result.output is not None else None,
                result.snapshot_id,
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
        rows = self._conn.execute(
            f"SELECT * FROM actions {where} ORDER BY timestamp DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        return [dict(row) for row in rows]

    def stats(self) -> dict:
        row = self._conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(blocked) as blocked,
                SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors,
                SUM(cost) as total_cost,
                COUNT(DISTINCT tool_name) as unique_tools
               FROM actions"""
        ).fetchone()
        return dict(row)

    def export_json(self, path: str | Path) -> int:
        rows = self._conn.execute(
            "SELECT * FROM actions ORDER BY timestamp"
        ).fetchall()
        data = [dict(r) for r in rows]
        Path(path).write_text(json.dumps(data, indent=2, default=str))
        return len(data)

    def close(self) -> None:
        self._conn.close()
