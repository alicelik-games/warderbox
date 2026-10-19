"""Tests for Phase 6: data masking, action replay."""

import json
import os
import sys
import tempfile

sys.path.insert(0, "src")

from warderbox import WarderBox, DataMasker
from warderbox.masking import DataMasker, DEFAULT_PATTERNS
from warderbox.replay import ActionReplayer


# -- Data Masking --

def test_mask_email():
    masker = DataMasker()
    result = masker.mask_string("Contact user@example.com for details")
    assert "user@example.com" not in result
    assert "u***@***.com" in result


def test_mask_api_key():
    masker = DataMasker()
    result = masker.mask_string("Key: sk-abc123def456ghi789jkl012mno345")
    assert "sk-abc123" not in result
    assert "sk-[REDACTED]" in result


def test_mask_bearer_token():
    masker = DataMasker()
    result = masker.mask_string("Authorization: Bearer eyJhbGciOiJIUz.token.here")
    assert "Bearer [REDACTED]" in result


def test_mask_credit_card():
    masker = DataMasker()
    result = masker.mask_string("Card: 4111 1111 1111 1111")
    assert "4111 1111" not in result
    assert "1111" in result


def test_mask_ssn():
    masker = DataMasker()
    result = masker.mask_string("SSN: 123-45-6789")
    assert "123-45-6789" not in result
    assert "***-**-****" in result


def test_mask_jwt():
    masker = DataMasker()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
    result = masker.mask_string(f"Token: {jwt}")
    assert "[JWT_REDACTED]" in result


def test_mask_dict():
    masker = DataMasker()
    data = {
        "email": "admin@company.com",
        "query": "SELECT * FROM users",
        "nested": {"token": "sk-abc123def456ghi789jkl012mno345pqr"},
    }
    masked = masker.mask(data)
    assert "admin@company.com" not in str(masked)
    assert "SELECT * FROM users" in masked["query"]
    assert "sk-[REDACTED]" in masked["nested"]["token"]


def test_mask_disabled():
    masker = DataMasker(enabled=False)
    text = "email: user@example.com"
    assert masker.mask_string(text) == text


def test_detect_patterns():
    masker = DataMasker()
    detected = masker.detect("Contact user@example.com with key sk-abc123def456ghi789jkl012")
    assert "email" in detected
    assert "api_key_openai" in detected


def test_custom_pattern():
    masker = DataMasker()
    masker.add_pattern("project_id", r"PROJ-\d{6}", "PROJ-******")
    result = masker.mask_string("Working on PROJ-123456")
    assert "PROJ-123456" not in result
    assert "PROJ-******" in result


def test_masker_in_shield_logs():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        masker = DataMasker()
        shield = WarderBox(budget=10.0, db_path=db_path)
        shield.set_masker(masker)

        @shield.tool(cost=0.01)
        def search(query: str):
            return "ok"

        search(query="find user@secret.com with key sk-abcdefghijklmnopqrstuvwx")

        rows = shield._logger.query(limit=1)
        assert len(rows) == 1
        args_str = rows[0]["args"]
        assert "user@secret.com" not in args_str
        assert "sk-abcdefghijklmnopqrstuvwx" not in args_str

        shield._logger.close()
    finally:
        os.unlink(db_path)


# -- Action Replay --

def test_replay_basic():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def safe_query(sql: str):
            return f"result: {sql}"

        @shield.tool(cost=0.01)
        def danger_query(sql: str):
            return f"result: {sql}"

        r1 = safe_query(sql="SELECT 1")
        r2 = danger_query(sql="DROP TABLE users")

        shield._logger.close()

        replay_shield = WarderBox(budget=10.0)
        replayer = ActionReplayer(
            db_path=db_path,
            executors={
                "safe_query": lambda sql: f"result: {sql}",
                "danger_query": lambda sql: f"result: {sql}",
            },
        )

        report = replayer.replay_all(replay_shield, limit=10)
        assert report.total == 2
        assert report.matched >= 1

        replayer.close()
    finally:
        os.unlink(db_path)


def test_replay_dry_run():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def ping(msg: str):
            return "pong"

        ping(msg="hello")
        ping(msg="world")
        shield._logger.close()

        replayer = ActionReplayer(db_path=db_path)
        plan = replayer.dry_run()
        assert len(plan) == 2
        assert all(p["tool"] == "ping" for p in plan)
        assert all(p["has_executor"] is False for p in plan)

        replayer.close()
    finally:
        os.unlink(db_path)


def test_replay_single_action():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        shield = WarderBox(budget=10.0, db_path=db_path)

        @shield.tool(cost=0.01)
        def calc(expr: str):
            return f"= {expr}"

        r = calc(expr="2+2")
        action_id = r.action.id
        shield._logger.close()

        replay_shield = WarderBox(budget=10.0)
        replayer = ActionReplayer(
            db_path=db_path,
            executors={"calc": lambda expr: f"= {expr}"},
        )

        result = replayer.replay_one(action_id, replay_shield)
        assert result.match
        assert result.tool_name == "calc"

        replayer.close()
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    test_mask_email()
    test_mask_api_key()
    test_mask_bearer_token()
    test_mask_credit_card()
    test_mask_ssn()
    test_mask_jwt()
    test_mask_dict()
    test_mask_disabled()
    test_detect_patterns()
    test_custom_pattern()
    test_masker_in_shield_logs()
    test_replay_basic()
    test_replay_dry_run()
    test_replay_single_action()
    print("All Phase 6 tests passed!")
