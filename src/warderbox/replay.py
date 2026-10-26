"""Action replay: re-execute logged actions for debugging and audit.

Reads action history from a SQLite audit log and replays them
through an WarderBox instance, comparing results.

Usage:
    from warderbox.replay import ActionReplayer

    replayer = ActionReplayer(
        db_path="audit.db",
        executors={"run_sql": my_sql_fn, "send_email": my_email_fn},
    )

    # Replay all actions and compare outcomes
    report = replayer.replay_all(shield)

    # Replay a single action by ID
    result = replayer.replay_one("abc123", shield)

    # Dry run — show what would be replayed without executing
    plan = replayer.dry_run()
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from warderbox.action import Action
from warderbox.logger import ActionLogger


@dataclass
class ReplayResult:
    """Result of replaying a single action."""
    action_id: str
    tool_name: str
    original_blocked: bool
    replayed_blocked: bool
    original_error: str | None
    replayed_error: str | None
    match: bool
    details: str


@dataclass
class ReplayReport:
    """Summary of replaying all actions."""
    total: int
    matched: int
    mismatched: int
    skipped: int
    results: list[ReplayResult]

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return self.matched / self.total


class ActionReplayer:
    """Replays logged actions through a shield for comparison."""

    def __init__(
        self,
        db_path: str | Path,
        executors: dict[str, Callable] | None = None,
    ) -> None:
        self._logger = ActionLogger(db_path)
        self._executors = executors or {}

    def register_executor(self, name: str, func: Callable) -> None:
        self._executors[name] = func

    def dry_run(self, limit: int = 100) -> list[dict]:
        """Show what actions would be replayed without executing."""
        rows = self._logger.query(limit=limit)
        plan = []
        for row in rows:
            plan.append({
                "id": row["id"],
                "tool": row["tool_name"],
                "args": row["args"][:100],
                "risk": row["risk"],
                "blocked": bool(row["blocked"]),
                "has_executor": row["tool_name"] in self._executors,
            })
        return plan

    def replay_one(self, action_id: str, shield) -> ReplayResult:
        """Replay a single action by its ID."""
        rows = self._logger._conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchall()

        if not rows:
            raise ValueError(f"Action '{action_id}' not found in log")

        row = dict(rows[0])
        return self._replay_row(row, shield)

    def replay_all(self, shield, limit: int = 100) -> ReplayReport:
        """Replay all logged actions and compare with original results."""
        rows = self._logger.query(limit=limit)
        results = []
        matched = 0
        mismatched = 0
        skipped = 0

        for row in rows:
            tool_name = row["tool_name"]
            if tool_name not in self._executors:
                skipped += 1
                continue

            result = self._replay_row(row, shield)
            results.append(result)
            if result.match:
                matched += 1
            else:
                mismatched += 1

        return ReplayReport(
            total=len(results),
            matched=matched,
            mismatched=mismatched,
            skipped=skipped,
            results=results,
        )

    def _replay_row(self, row: dict, shield) -> ReplayResult:
        tool_name = row["tool_name"]
        original_blocked = bool(row["blocked"])
        original_error = row.get("error")

        args = json.loads(row["args"]) if isinstance(row["args"], str) else row["args"]

        executor = self._executors.get(tool_name)
        if executor is None:
            return ReplayResult(
                action_id=row["id"],
                tool_name=tool_name,
                original_blocked=original_blocked,
                replayed_blocked=False,
                original_error=original_error,
                replayed_error=None,
                match=False,
                details=f"No executor for '{tool_name}'",
            )

        # Legacy / signature-less captures store positional args as
        # {"args": [...]}; unpack those positionally instead of as kwargs.
        if (
            isinstance(args, dict)
            and set(args.keys()) == {"args"}
            and isinstance(args["args"], list)
        ):
            call_args, call_kwargs = tuple(args["args"]), {}
        else:
            call_args, call_kwargs = (), args

        action = Action(tool_name=tool_name, args=args, cost=row.get("cost", 0))
        result = shield._execute(
            action=action,
            func=executor,
            args=call_args,
            kwargs=call_kwargs,
            cost=row.get("cost", 0),
            risk_override=None,
        )

        match = original_blocked == result.blocked
        if original_error and result.error:
            match = match and True
        elif original_error != (result.error is not None and result.error or None):
            match = False

        details = "OK" if match else (
            f"Mismatch: original_blocked={original_blocked}, "
            f"replayed_blocked={result.blocked}"
        )

        return ReplayResult(
            action_id=row["id"],
            tool_name=tool_name,
            original_blocked=original_blocked,
            replayed_blocked=result.blocked,
            original_error=original_error,
            replayed_error=result.error,
            match=match,
            details=details,
        )

    def close(self) -> None:
        self._logger.close()
