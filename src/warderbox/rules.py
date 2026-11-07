"""Risk classification rules for agent actions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from warderbox.action import Action, RiskLevel

_IDENT = r"[\w.$\"`\[\]]+"
_DDL_OBJECT = (
    r"(?:TABLE|DATABASE|SCHEMA|VIEW|INDEX|SEQUENCE|FUNCTION|PROCEDURE|TRIGGER"
    r"|TYPE|ROLE|USER|COLLECTION|EVENT|DOMAIN|EXTENSION)"
)
_DROP = (
    r"DROP\s+(?:(?:TEMPORARY|TEMP|MATERIALIZED|FOREIGN|GLOBAL|LOCAL|EXTERNAL"
    rf"|UNIQUE|CLUSTERED|NONCLUSTERED)\s+)*{_DDL_OBJECT}\b"
)
_ALTER_DROP = (
    rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_IDENT}\s+DROP\b"
)
_TRUNCATE = rf"TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?{_IDENT}"
_DELETE = (
    r"DELETE\s+(?:(?:LOW_PRIORITY|QUICK|IGNORE)\s+)*"
    rf"(?:{_IDENT}\s+)?FROM\s+(?:ONLY\s+)?{_IDENT}"
)
_DELETE_BARE = rf"DELETE\s+(?!FROM\b){_IDENT}"
_UPDATE = (
    r"UPDATE\s+(?:(?:LOW_PRIORITY|IGNORE|ONLY)\s+)*"
    rf"{_IDENT}(?:\s+(?:AS\s+)?\w+)?\s+SET\b"
)
_RISKY = (
    r"RENAME\s+TABLE\b"
    rf"|ALTER\s+TABLE\s+{_IDENT}\s+RENAME\b"
    r"|GRANT\s+.+?\b(?:ON|TO)\b"
    r"|REVOKE\s+.+?\b(?:ON|FROM)\b"
    r"|MERGE\s+INTO\b.*?\bDELETE\b"
)
_DYNAMIC = (
    r"\b(?:EXEC|EXECUTE)\s*\(|\bEXECUTE\s+IMMEDIATE\b"
    r"|\bPREPARE\s+\w+\s+FROM\b|\bsp_executesql\b"
)

_FLAGS = re.IGNORECASE | re.DOTALL
_DROP_RE = re.compile(rf"\b(?:{_DROP}|{_ALTER_DROP})", _FLAGS)
_TRUNCATE_RE = re.compile(rf"\b{_TRUNCATE}", _FLAGS)
_DELETE_RE = re.compile(rf"\b(?:{_DELETE}|{_DELETE_BARE})", _FLAGS)
_UPDATE_RE = re.compile(rf"\b{_UPDATE}", _FLAGS)
_RISKY_RE = re.compile(rf"\b(?:{_RISKY})", _FLAGS)
_DYNAMIC_RE = re.compile(_DYNAMIC, _FLAGS)
_WHERE_RE = re.compile(r"\bWHERE\b", _FLAGS)
# A WHERE that matches every row is the same as no WHERE. Leading tautologies
# followed by AND (query-builder style "WHERE 1=1 AND id=5") stay filtering.
_TAUTOLOGY_RE = re.compile(
    r"\b(?:WHERE|OR)\s+\(*\s*(?:TRUE|NOT\s+FALSE|1|''\s*=\s*''|\"\"\s*=\s*\"\""
    r"|([\w.]{1,128})\s*(?:=|<=|>=)\s*\1)\s*\)*\s*(?=$|;|\bOR\b)",
    _FLAGS,
)

# Outside a known SQL field, text that reads as a statement counts wherever it
# appears, so `psql -c 'DELETE FROM users'` and `echo DROP TABLE x | mysql` are
# caught while prose like "truncate logs later" is not treated as SQL.
_ANCHOR = r"(?:^|[;\n'\"`(]|\s-[ce]\s)\s*"
_CLAUSE = (
    r"(?:WHERE|USING|AS|ORDER|LIMIT|RETURNING|OUTPUT|CASCADE|RESTRICT|RESTART"
    r"|CONTINUE|PURGE)"
)
_END = rf"\s*(?=$|;|,|['\"`)|]|\b{_CLAUSE}\b)"
_SHAPED_DROP_RE = re.compile(
    r"\bDROP\s+(?:(?:TEMPORARY|TEMP|EXTERNAL)\s+)*(?:TABLE|DATABASE|SCHEMA)\b"
    rf"|{_ANCHOR}(?:{_DROP}\s+(?:IF\s+EXISTS\s+)?{_IDENT}"
    rf"(?:\s*,\s*{_IDENT})*(?:\s+(?:CASCADE|RESTRICT|PURGE))?{_END}|{_ALTER_DROP})",
    _FLAGS,
)
_SHAPED_TRUNCATE_RE = re.compile(
    r"\bTRUNCATE\s+TABLE\b"
    rf"|\bTRUNCATE\s+(?:ONLY\s+)?{_IDENT}(?:\s*,\s*{_IDENT})*{_END}",
    _FLAGS,
)
_SHAPED_DELETE_RE = re.compile(
    r"\bDELETE\s+(?:(?:LOW_PRIORITY|QUICK|IGNORE)\s+)*"
    rf"(?:{_IDENT}\s+)?FROM\s+(?:ONLY\s+)?{_IDENT}"
    rf"(?:{_END}|\s+(?:AS\s+)?\w+\s+(?=\b{_CLAUSE}\b))",
    _FLAGS,
)
_SHAPED_UPDATE_RE = re.compile(rf"\b{_UPDATE}", _FLAGS)
# In free text (an email body) only a complete DROP/ALTER statement counts.
_PROSE_STATEMENT_RE = re.compile(
    rf"(?:^|;)\s*(?:{_DROP}|{_ALTER_DROP})\s+(?:IF\s+EXISTS\s+)?{_IDENT}"
    rf"(?:\s+{_IDENT})?\s*(?:;|$)",
    _FLAGS,
)

_SQL_TOOL_RE = re.compile(
    r"(sql|query|database|postgres|mysql|sqlite|bigquery|snowflake|redshift|warehouse)",
    re.IGNORECASE,
)
_SQL_KEYS = {"sql", "sql_query", "raw_sql", "statement", "stmt", "script", "ddl", "dml"}
# A search tool's "query" is what someone is looking for, not code to run.
_SEARCH_TOOL_RE = re.compile(r"search|lookup|look_up|find|browse|web|retriev|google|bing", re.IGNORECASE)
# Elsewhere "query" is SQL when it starts like SQL. Statements that are also
# English ("delete from my calendar") must instead read as a statement.
_SQL_START_RE = re.compile(
    r"\s*(?:/\*.*?\*/\s*)*\(?\s*(?:SELECT|INSERT|REPLACE|WITH|GRANT|REVOKE|RENAME"
    r"|ALTER|MERGE|EXEC|EXECUTE|CALL|CREATE|EXPLAIN|BEGIN)\b",
    re.IGNORECASE | re.DOTALL,
)
_PROSE_KEYS = {
    "body", "message", "text", "subject", "content", "title", "description",
    "note", "notes", "comment", "prompt", "summary", "html",
}
_MESSAGING_TOOLS = {"send_email", "send_message", "send_slack", "post_message"}
_MAX_DEPTH = 32
_MAX_TOKENIZED_CHARS = 256_000
_MAX_PARSED_CHARS = 100_000

DESTRUCTIVE = "destructive"
UPDATE_NO_WHERE = "update_no_where"
RISKY = "risky"


@dataclass
class Rule:
    """A single rule that maps an action pattern to a risk level."""

    name: str
    check: Callable[[Action], bool]
    risk: RiskLevel
    reason: str


def _iter_fields(value: object, path: tuple[str, ...] = (), depth: int = 0, seen=None):
    """Yield ``(key_path, text)`` for every string reachable from *value*."""
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        yield path, bytes(value).decode("utf-8", "ignore")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    seen = set() if seen is None else seen
    if depth >= _MAX_DEPTH or id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield path, k
            yield from _iter_fields(v, (*path, str(k).lower()), depth + 1, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _iter_fields(v, path, depth + 1, seen)
    else:
        # Query objects (SQLAlchemy text(), ORM statements, ...) carry their
        # SQL in __str__, which is what the database eventually receives.
        try:
            yield path, str(value)
        except Exception:
            return


def _iter_strings(value: object):
    """Yield every string anywhere inside a (possibly nested) value."""
    for _path, text in _iter_fields(value):
        yield text


def _sanitize_sql(
    text: str,
    *,
    backslash_escapes: bool,
    hash_comments: bool,
    loose_dashes: bool,
    blank_literals: bool = True,
) -> tuple[str, bool]:
    """Strip comments and blank string literals; report whether quotes balance.

    Dialects disagree on what is a comment or an escaped quote, and each
    disagreement is a potential bypass, so callers scan every interpretation.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        pair = text[i:i + 2]
        if pair == "/*":
            close = text.find("*/", i + 2)
            inner = text[i + 2:] if close == -1 else text[i + 2:close]
            # MySQL executes the body of /*! ... */ version comments.
            if inner[:1] == "!" or inner[:2].upper() == "M!":
                out.append(" " + re.sub(r"^M?!\d*", "", inner, flags=re.IGNORECASE) + " ")
            else:
                out.append(" ")
            i = n if close == -1 else close + 2
        elif pair == "--" and (loose_dashes or text[i + 2:i + 3] in ("", " ", "\t", "\n", "\r")) or c == "#" and hash_comments:
            newline = text.find("\n", i)
            i = n if newline == -1 else newline
            out.append(" ")
        elif c in ("'", '"', "`"):
            j = i + 1
            closed = False
            while j < n:
                if backslash_escapes and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    if text[j + 1:j + 2] == c:
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            if not closed:
                out.append(text[i:] if not blank_literals else c)
                return "".join(out), False
            out.append(c + c if blank_literals else text[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), True


def _sql_variants(text: str) -> set[str]:
    """Every reading of *text* a real database might execute."""
    if len(text) > _MAX_TOKENIZED_CHARS:
        # Too large to tokenize on every call: scan the text as it stands. This
        # can only over-report (keywords inside literals still count).
        return {re.sub(r"/\*!?\d*|\*/", " ", text)}
    variants: set[str] = set()
    needs_literals = bool(_DYNAMIC_RE.search(text))
    # Only try readings the text can actually differ under.
    backslash_options = (True, False) if "\\" in text else (False,)
    hash_options = (True, False) if "#" in text else (False,)
    dash_options = (True, False) if "--" in text else (False,)
    for backslash in backslash_options:
        for hashes in hash_options:
            for dashes in dash_options:
                options = {
                    "backslash_escapes": backslash,
                    "hash_comments": hashes,
                    "loose_dashes": dashes,
                }
                code, balanced = _sanitize_sql(text, **options)
                variants.add(code)
                if not balanced or needs_literals:
                    variants.add(_sanitize_sql(text, blank_literals=False, **options)[0])
    return variants


def _classify_statement(stmt: str) -> set[str]:
    labels: set[str] = set()
    if _DROP_RE.search(stmt) or _TRUNCATE_RE.search(stmt):
        labels.add(DESTRUCTIVE)
    if _DELETE_RE.search(stmt):
        labels.add(DESTRUCTIVE)
    has_real_where = _WHERE_RE.search(stmt) and not _TAUTOLOGY_RE.search(stmt)
    if _UPDATE_RE.search(stmt) and not has_real_where:
        labels.add(UPDATE_NO_WHERE)
    if _RISKY_RE.search(stmt):
        labels.add(RISKY)
    return labels


def _ast_labels(text: str) -> set[str]:
    """Classify with sqlglot when installed; regex remains the safety net."""
    if len(text) > _MAX_PARSED_CHARS:
        return set()
    try:
        import logging

        import sqlglot
        from sqlglot import exp
    except ImportError:
        return set()
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    try:
        trees = sqlglot.parse(text, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return set()

    def matches_all(where) -> bool:
        if where is None:
            return True
        cond = where.this
        while isinstance(cond, exp.Paren):
            cond = cond.this
        if isinstance(cond, exp.Or):
            return matches_all(exp.Where(this=cond.left)) or matches_all(
                exp.Where(this=cond.right)
            )
        if isinstance(cond, exp.Boolean):
            return bool(cond.this)
        if isinstance(cond, exp.Literal):
            return cond.this not in ("0", "")
        if isinstance(cond, (exp.EQ, exp.GTE, exp.LTE)):
            return cond.left.sql() == cond.right.sql()
        return False

    alter = getattr(exp, "Alter", None) or getattr(exp, "AlterTable", None)
    labels: set[str] = set()
    for tree in trees:
        if tree is None:
            continue
        for node in tree.walk():
            if isinstance(node, (exp.Drop, exp.TruncateTable)):
                labels.add(DESTRUCTIVE)
            elif alter is not None and isinstance(node, alter):
                if any(isinstance(a, exp.Drop) for a in node.args.get("actions") or []):
                    labels.add(DESTRUCTIVE)
            elif isinstance(node, exp.Delete):
                labels.add(DESTRUCTIVE)
            elif isinstance(node, exp.Update):
                if matches_all(node.args.get("where")):
                    labels.add(UPDATE_NO_WHERE)
            elif isinstance(node, exp.Merge) and node.find(exp.Delete):
                labels.add(RISKY)
    return labels


@lru_cache(maxsize=2048)
def _classify_sql(text: str) -> frozenset[str]:
    """Classify text known to be SQL. Regex and parser results are unioned."""
    labels: set[str] = set()
    for code in _sql_variants(text):
        for stmt in code.split(";"):
            labels |= _classify_statement(stmt)
    if _DYNAMIC_RE.search(text):
        labels.add(RISKY)
    return frozenset(labels | _ast_labels(text))


_STATEMENT_END_RE = re.compile(r"[;'\"`)]")


@lru_cache(maxsize=2048)
def _classify_unknown(text: str) -> frozenset[str]:
    """Classify a field of unknown type: only statement-shaped text counts."""
    labels: set[str] = set()
    code = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    if (
        _SHAPED_DROP_RE.search(code)
        or _SHAPED_TRUNCATE_RE.search(code)
        or _SHAPED_DELETE_RE.search(code)
    ):
        labels.add(DESTRUCTIVE)
    # Each statement ends at the next UPDATE at the latest, so the total work
    # stays linear however many there are.
    starts = [m.start() for m in _SHAPED_UPDATE_RE.finditer(code)]
    for start, limit in zip(starts, [*starts[1:], len(code)], strict=False):
        end = _STATEMENT_END_RE.search(code, start, limit)
        stmt = code[start:end.start() if end else limit]
        if not _WHERE_RE.search(stmt) or _TAUTOLOGY_RE.search(stmt):
            labels.add(UPDATE_NO_WHERE)
            break
    return frozenset(labels)


def _sql_labels(action: Action) -> set[str]:
    labels: set[str] = set()
    search_tool = bool(_SEARCH_TOOL_RE.search(action.tool_name))
    sql_tool = bool(_SQL_TOOL_RE.search(action.tool_name)) and not search_tool
    messaging = action.tool_name in _MESSAGING_TOOLS
    for path, text in _iter_fields(action.args):
        if (
            sql_tool
            or any(key in _SQL_KEYS for key in path)
            or ("query" in path and not search_tool and _SQL_START_RE.match(text))
        ):
            labels |= _classify_sql(text)
        elif messaging:
            code = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
            if _PROSE_STATEMENT_RE.search(code):
                labels.add(DESTRUCTIVE)
        elif path and path[-1] in _PROSE_KEYS:
            labels |= _classify_unknown(text) - {UPDATE_NO_WHERE}
        else:
            labels |= _classify_unknown(text)
    return labels


def _check_destructive_sql(action: Action) -> bool:
    return DESTRUCTIVE in _sql_labels(action)


def _check_bulk_update_sql(action: Action) -> bool:
    return UPDATE_NO_WHERE in _sql_labels(action)


def _check_risky_sql(action: Action) -> bool:
    return RISKY in _sql_labels(action)


def _iter_ints(value: object, keys: set[str], depth: int = 0, seen=None):
    """Yield ints stored under any of *keys*, recursing through containers."""
    if not isinstance(value, (dict, list, tuple, set, frozenset)):
        return
    seen = set() if seen is None else seen
    if depth >= _MAX_DEPTH or id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, dict):
        for k, v in value.items():
            if k in keys and isinstance(v, bool) is False and isinstance(v, int):
                yield v
            else:
                yield from _iter_ints(v, keys, depth + 1, seen)
    else:
        for v in value:
            yield from _iter_ints(v, keys, depth + 1, seen)


def _check_file_delete(action: Action) -> bool:
    return action.tool_name in ("delete_file", "remove_file", "rm", "unlink")


def _check_send_message(action: Action) -> bool:
    return action.tool_name in ("send_email", "send_message", "send_slack", "post_message")


def _check_bulk_operation(action: Action) -> bool:
    return any(n > 100 for n in _iter_ints(action.args, {"count", "limit"}))


DEFAULT_RULES: list[Rule] = [
    Rule(
        name="destructive_sql",
        check=_check_destructive_sql,
        risk=RiskLevel.CRITICAL,
        reason="Destructive SQL detected (DROP/TRUNCATE/DELETE)",
    ),
    Rule(
        name="bulk_update_no_where",
        check=_check_bulk_update_sql,
        risk=RiskLevel.HIGH,
        reason="Bulk UPDATE without WHERE clause",
    ),
    Rule(
        name="risky_sql",
        check=_check_risky_sql,
        risk=RiskLevel.HIGH,
        reason="Schema, privilege or dynamic SQL change requires review",
    ),
    Rule(
        name="file_delete",
        check=_check_file_delete,
        risk=RiskLevel.HIGH,
        reason="File deletion requested",
    ),
    Rule(
        name="send_message",
        check=_check_send_message,
        risk=RiskLevel.MEDIUM,
        reason="Outbound message (email/chat)",
    ),
    Rule(
        name="bulk_operation",
        check=_check_bulk_operation,
        risk=RiskLevel.HIGH,
        reason="Bulk operation (>100 items)",
    ),
]


@dataclass
class RuleEngine:
    """Evaluates actions against a set of rules and returns the highest risk level."""

    rules: list[Rule] = field(default_factory=lambda: list(DEFAULT_RULES))

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)

    def evaluate(self, action: Action) -> tuple[RiskLevel, list[str]]:
        """Returns (highest_risk_level, list_of_triggered_reasons)."""
        triggered: list[str] = []
        highest = RiskLevel.LOW

        for rule in self.rules:
            if rule.check(action):
                triggered.append(rule.reason)
                if list(RiskLevel).index(rule.risk) > list(RiskLevel).index(highest):
                    highest = rule.risk

        return highest, triggered
