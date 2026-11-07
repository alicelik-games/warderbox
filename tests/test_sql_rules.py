"""SQL risk detection: bypasses that must be caught, prose that must not be."""

from __future__ import annotations

import time

import pytest

from warderbox.action import Action, RiskLevel
from warderbox.rules import RuleEngine

engine = RuleEngine()


def risk(tool: str, **args) -> RiskLevel:
    return engine.evaluate(Action(tool_name=tool, args=args))[0]


@pytest.mark.parametrize("sql", [
    "DROP/**/TABLE users",
    "drop\ntable users",
    "DROP -- c\nTABLE users",
    "DROP#c\nTABLE users",
    "DROP/*!50000 TABLE*/ users",
    "DROP TEMPORARY TABLE t",
    "DROP MATERIALIZED VIEW v",
    "ALTER TABLE a.b DROP c",
    "TRUNCATE users",
    "TRUNCATE ONLY users",
    "DELETE FROM users",
    "DELETE/**/FROM users",
    "DELETE FROM users WHERE id=1",
    "DELETE FROM users WHERE id IN (SELECT id FROM t)",
    "delete from users where id=1",
    "WITH d AS (DELETE FROM users WHERE id=1 RETURNING *) SELECT * FROM d",
    "DELETE FROM users WHERE 1=1",
    "DELETE FROM users WHERE id=1 OR 1=1",
    "DELETE FROM users WHERE true",
    "DELETE FROM users WHERE 'a'='a'",
    "DELETE FROM users -- WHERE x=1",
    "DELETE FROM users; SELECT 1 WHERE 1=1",
    "DELETE u FROM users u",
    'DELETE FROM "users"',
    "EXEC('DROP TABLE users')",
    "SELECT 1; DROP TABLE users",
    "SELECT 'a\\'; DROP TABLE x; --'",
    "SELECT a # b; DROP TABLE x",
    "SELECT 5--1; DROP TABLE x",
    "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
])
def test_destructive_sql_is_critical(sql):
    assert risk("query_db", sql=sql) is RiskLevel.CRITICAL


@pytest.mark.parametrize(("tool", "args"), [
    ("execute", {"command": "DELETE FROM users"}),
    ("run", {"q": "DELETE FROM users;"}),
    ("run_command", {"cmd": "psql -c 'DELETE FROM users'"}),
    ("run_command", {"cmd": 'mysql -e "DROP TABLE users"'}),
    ("search", {"q": "DROP TABLE users"}),
    ("t", {"data": b"DROP TABLE users"}),
    ("run", {"payload": {"a": [{"q": "TRUNCATE users"}]}}),
    ("run", {"DROP TABLE users": 1}),
    ("run", {"items": ("DROP TABLE users",)}),
])
def test_statement_in_any_field_is_critical(tool, args):
    assert risk(tool, **args) is RiskLevel.CRITICAL


def test_object_str_is_scanned():
    class Query:
        def __str__(self):
            return "DROP TABLE users"

    assert risk("run", x=Query()) is RiskLevel.CRITICAL


def test_self_referencing_args_do_not_hang():
    loop: dict = {}
    loop["self"] = loop
    assert risk("run", x=loop) is RiskLevel.LOW


@pytest.mark.parametrize("sql", [
    "UPDATE users SET a=1",
    "UPDATE users SET a=1 -- WHERE x",
    "UPDATE public.users SET a=1",
    'UPDATE "users" SET a=1',
    "UPDATE users SET a=1 WHERE 1=1",
    "RENAME TABLE a TO b",
    "MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN DELETE",
    "GRANT ALL ON db TO x",
    "EXEC('SELECT 1')",
])
def test_risky_sql_is_high(sql):
    assert risk("query_db", sql=sql) is RiskLevel.HIGH


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT * FROM logs WHERE msg = 'DROP TABLE x'",
    "SELECT 'DELETE FROM users'",
    "UPDATE users SET a=1 WHERE id=2",
    "SELECT * FROM t WHERE 1=1 AND id=5",
    "INSERT INTO a VALUES ('TRUNCATE x')",
])
def test_harmless_sql_is_not_flagged(sql):
    assert risk("query_db", sql=sql) in (RiskLevel.LOW, RiskLevel.MEDIUM)


@pytest.mark.parametrize("body", [
    "please truncate the log",
    "delete from my calendar",
    "we should truncate logs later",
    "delete from inbox",
    "Please DROP TABLE tennis practice",
    "Update users set up",
])
def test_prose_in_email_is_not_critical(body):
    assert risk("send_email", body=body) is RiskLevel.MEDIUM


@pytest.mark.parametrize("q", [
    "how to delete from a list in python",
    "truncate logs later",
    "update user settings",
])
def test_prose_in_unknown_field_is_not_flagged(q):
    assert risk("web_search", q=q) is RiskLevel.LOW


def test_complete_statement_in_prose_field_is_still_critical():
    assert risk("note_tool", body="DROP TABLE users;") is RiskLevel.CRITICAL


@pytest.mark.parametrize("text", [
    "DELETE " + "a" * 300_000,
    "DROP" + " " * 300_000 + "x",
    "DROP " * 60_000,
    "GRANT " + "x " * 150_000,
    "MERGE INTO " + "x " * 150_000,
    "'" * 300_001,
    "SELECT 1 -- x\n" * 25_000,
], ids=["ident", "spaces", "drop", "grant", "merge", "quotes", "comments"])
@pytest.mark.parametrize(("tool", "key"), [
    ("query_db", "sql"), ("run", "q"), ("send_email", "body"),
])
def test_rule_time_stays_bounded_on_hostile_text(text, tool, key):
    start = time.perf_counter()
    risk(tool, **{key: text})
    assert time.perf_counter() - start < 10


def test_destructive_statement_past_the_tokenizing_limit_is_still_caught():
    padding = "SELECT 1; " * 40_000
    assert risk("query_db", sql=padding + "DROP TABLE users") is RiskLevel.CRITICAL


@pytest.mark.parametrize(("tool", "args"), [
    ("run", {"query": "TRUNCATE a"}),
    ("run", {"query": "DELETE FROM a WHERE x>0"}),
    ("run", {"query": "delete from a where b=1 returning *"}),
    ("run", {"query": "DELETE FROM a USING b WHERE a.i=b.i"}),
    ("run", {"cmd": "TRUNCATE a"}),
    ("run", {"cmd": "TRUNCATE users CASCADE"}),
    ("run", {"cmd": "TRUNCATE a, b"}),
    ("run", {"cmd": "TRUNCATE users RESTART IDENTITY"}),
    ("run", {"cmd": "DELETE FROM a WHERE x>0"}),
    ("run", {"cmd": "DELETE FROM users u WHERE u.id=1"}),
    ("run", {"cmd": "DELETE FROM users AS u WHERE u.id=1"}),
    ("run", {"cmd": "DELETE FROM a USING b WHERE a.i=b.i"}),
    ("run", {"cmd": "DELETE FROM a ORDER BY x LIMIT 1"}),
    ("run", {"cmd": "DELETE FROM a LIMIT 5"}),
    ("run", {"cmd": "DROP TABLE users please"}),
    ("run", {"cmd": "echo DROP TABLE users | mysql"}),
    ("run", {"cmd": "echo DELETE FROM users | psql"}),
    ("run", {"cmd": "sql=DELETE FROM users"}),
    ("run", {"cmd": "run this: TRUNCATE TABLE users"}),
    ("shell", {"cmd": "psql -c 'DELETE FROM a WHERE x=1'"}),
    ("run", {"text": "DELETE FROM users"}),
    ("run", {"content": "TRUNCATE users"}),
    ("run", {"note": "DROP TABLE users"}),
])
def test_statements_outside_sql_fields_stay_blocked(tool, args):
    assert risk(tool, **args) is RiskLevel.CRITICAL


@pytest.mark.parametrize("tool", ["web_search", "search", "lookup", "search_query"])
@pytest.mark.parametrize("query", [
    "how to delete from a list in python",
    "truncate logs later",
    "delete from my calendar",
    "grant writing tips to read",
    "update user settings",
])
def test_search_queries_are_not_treated_as_sql(tool, query):
    assert risk(tool, query=query) is RiskLevel.LOW


@pytest.mark.parametrize("tool", ["web_search", "lookup"])
@pytest.mark.parametrize("query", [
    "DROP TABLE users",
    "DELETE FROM users",
    "DELETE FROM users WHERE id=1",
    "TRUNCATE TABLE users",
    "SELECT 1; DROP TABLE users",
])
def test_statements_in_search_queries_stay_blocked(tool, query):
    assert risk(tool, query=query) is RiskLevel.CRITICAL


def test_explicit_sql_field_on_a_search_tool_is_sql():
    assert risk("search_logs", sql="DELETE FROM a WHERE x=1") is RiskLevel.CRITICAL


@pytest.mark.parametrize("query", [
    "GRANT ALL ON a TO b",
    "RENAME TABLE a TO b",
])
def test_query_argument_is_treated_as_sql(query):
    assert risk("run", query=query) is RiskLevel.HIGH


def test_literal_in_query_argument_is_not_a_statement():
    assert risk("run", query="SELECT 'DROP TABLE users'") is RiskLevel.LOW


@pytest.mark.parametrize("text", [
    "UPDATE a SET " * 150_000,
    "UPDATE a SET b=1 WHERE c " * 80_000,
    "DELETE FROM a " * 150_000,
    "TRUNCATE " + "a," * 1_000_000,
    "DROP TABLE " * 200_000,
    "delete " * 300_000,
    "DELETE" + " " * 2_000_000,
])
@pytest.mark.parametrize("key", ["cmd", "sql", "text"])
def test_hostile_input_stays_fast(key, text):
    start = time.perf_counter()
    risk("run", **{key: text})
    assert time.perf_counter() - start < 5


def test_each_update_is_judged_on_its_own():
    assert risk("run", cmd="UPDATE a SET b=1 WHERE c=2; UPDATE d SET e=1") is RiskLevel.HIGH
    assert risk("run", cmd="UPDATE a SET b=1 UPDATE d SET e=1 WHERE c=2") is RiskLevel.HIGH
    assert risk("run", cmd="UPDATE a SET b=1 WHERE c=2") is RiskLevel.LOW
