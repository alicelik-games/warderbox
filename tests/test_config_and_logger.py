"""Tests for config, custom rules, and SQLite logger."""

import os
import sys
import tempfile

sys.path.insert(0, "src")

from warderbox import WarderBox


def test_from_config_custom_rule():
    shield = WarderBox.from_config({
        "budget": 10.0,
        "rules": [
            {
                "name": "block_prod",
                "match": {"args_contain": "production"},
                "risk": "critical",
                "reason": "Production access blocked",
            }
        ],
    })

    @shield.tool(cost=0.01)
    def run_query(sql: str):
        return "ok"

    safe = run_query(sql="SELECT * FROM staging.users")
    assert not safe.blocked

    blocked = run_query(sql="SELECT * FROM production.users")
    assert blocked.blocked
    assert "Production access blocked" in blocked.block_reason


def test_from_config_regex_rule():
    shield = WarderBox.from_config({
        "rules": [
            {
                "name": "no_passwords",
                "match": {"args_regex": r"password|secret|token"},
                "risk": "critical",
                "reason": "Credential access blocked",
            }
        ],
    })

    @shield.tool(cost=0.01)
    def search(query: str):
        return "results"

    safe = search(query="find user by name")
    assert not safe.blocked

    blocked = search(query="find user password")
    assert blocked.blocked


def test_sqlite_logger():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.05)
        def do_work(task: str):
            return f"done: {task}"

        @shield.tool(cost=0.01)
        def run_sql(query: str):
            return "ok"

        do_work(task="clean data")
        do_work(task="process results")
        run_sql(query="DROP TABLE important")  # should be blocked

        stats = shield._logger.stats()
        assert stats["total"] == 3
        assert stats["blocked"] == 1
        assert stats["total_cost"] > 0

        rows = shield._logger.query(blocked_only=True)
        assert len(rows) == 1
        assert "DROP TABLE" in rows[0]["args"]

        export_path = db_path + ".json"
        count = shield._logger.export_json(export_path)
        assert count == 3

        shield._logger.close()
    finally:
        os.unlink(db_path)
        if os.path.exists(export_path):
            os.unlink(export_path)


if __name__ == "__main__":
    test_from_config_custom_rule()
    test_from_config_regex_rule()
    test_sqlite_logger()
    print("All config & logger tests passed!")
