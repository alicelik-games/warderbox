"""Risk classification rules for agent actions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from warderbox.action import Action, RiskLevel

# SQL patterns that indicate destructive operations
_DESTRUCTIVE_SQL = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE|DELETE\s+FROM)\b",
    re.IGNORECASE,
)
_BULK_UPDATE_SQL = re.compile(
    r"\b(UPDATE\s+\w+\s+SET)\b(?!.*\bWHERE\b)",
    re.IGNORECASE | re.DOTALL,  # DOTALL so a WHERE on a later line still counts
)


@dataclass
class Rule:
    """A single rule that maps an action pattern to a risk level."""

    name: str
    check: Callable[[Action], bool]
    risk: RiskLevel
    reason: str


def _iter_strings(value: object):
    """Yield every string anywhere inside a (possibly nested) value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _iter_strings(v)


def _iter_ints(value: object, keys: set[str]):
    """Yield ints stored under any of *keys*, recursing through containers."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in keys and isinstance(v, bool) is False and isinstance(v, int):
                yield v
            else:
                yield from _iter_ints(v, keys)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _iter_ints(v, keys)


def _check_destructive_sql(action: Action) -> bool:
    return any(_DESTRUCTIVE_SQL.search(s) for s in _iter_strings(action.args))


def _check_bulk_update_sql(action: Action) -> bool:
    return any(_BULK_UPDATE_SQL.search(s) for s in _iter_strings(action.args))


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
                if rule.risk.value > highest.value or (
                    list(RiskLevel).index(rule.risk) > list(RiskLevel).index(highest)
                ):
                    highest = rule.risk

        return highest, triggered
