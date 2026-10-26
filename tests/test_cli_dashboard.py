"""Regression tests for CLI stats (P1-1) and dashboard DB hydration (P1-2)."""

from __future__ import annotations

import subprocess
import sys

from warderbox import WarderBox
from warderbox.logger import ActionLogger


def _populate(db_path: str) -> None:
    shield = WarderBox(budget=5.0, db_path=db_path)

    @shield.tool(cost=0.02)
    def lookup(q: str) -> str:
        return "ok"

    lookup(q="a")
    lookup(q="b")
    shield._logger.close()


# --- P1-1: CLI stats must not crash on empty or populated DB -----------------

def test_p1_1_cli_stats_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    proc = subprocess.run(
        [sys.executable, "-m", "warderbox", "--db", db, "stats"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Total actions:" in proc.stdout


def test_p1_1_cli_stats_populated_db(tmp_path):
    db = str(tmp_path / "pop.db")
    _populate(db)
    proc = subprocess.run(
        [sys.executable, "-m", "warderbox", "--db", db, "stats"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "lookup" in proc.stdout


def test_p1_1_logger_stats_has_by_tool(tmp_path):
    db = str(tmp_path / "pop.db")
    _populate(db)
    logger = ActionLogger(db)
    s = logger.stats()
    assert "by_tool" in s
    assert s["by_tool"]["lookup"]["calls"] == 2
    assert s["total_cost"] >= 0.0
    logger.close()


# --- P1-2: dashboard shield must reflect persisted actions -------------------

def test_p1_2_history_loads_from_db(tmp_path):
    db = str(tmp_path / "pop.db")
    _populate(db)

    fresh = WarderBox(db_path=db)
    count = fresh.load_history_from_db()
    assert count == 2
    assert fresh.stats()["total_actions"] == 2
    assert len(fresh.history()) == 2
