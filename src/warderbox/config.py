"""Configuration and custom rule definitions.

Allows users to define rules via dicts or YAML-like structures.

Usage:
    shield = WarderBox.from_config({
        "budget": 5.00,
        "mode": "production",
        "rules": [
            {
                "name": "no_prod_db",
                "match": {"tool": "run_sql", "args_contain": "production"},
                "risk": "critical",
                "reason": "Production database access not allowed",
            }
        ]
    })
"""

from __future__ import annotations

import re
from typing import Any

from warderbox.action import Action, RiskLevel
from warderbox.rules import Rule


def _make_checker(match: dict[str, Any]):
    """Build a check function from a match dict."""

    tool_pattern = match.get("tool")
    args_contain = match.get("args_contain")
    args_regex = match.get("args_regex")
    tool_in = match.get("tool_in")

    compiled_regex = re.compile(args_regex, re.IGNORECASE) if args_regex else None

    def check(action: Action) -> bool:
        if tool_pattern and action.tool_name != tool_pattern:
            return False
        if tool_in and action.tool_name not in tool_in:
            return False
        if args_contain:
            found = any(
                isinstance(v, str) and args_contain.lower() in v.lower()
                for v in action.args.values()
            )
            if not found:
                return False
        if compiled_regex:
            found = any(
                isinstance(v, str) and compiled_regex.search(v)
                for v in action.args.values()
            )
            if not found:
                return False
        return True

    return check


def rules_from_config(config_rules: list[dict[str, Any]]) -> list[Rule]:
    """Convert a list of rule dicts into Rule objects."""
    rules = []
    for cfg in config_rules:
        name = cfg.get("name", "custom_rule")
        match = cfg.get("match", {})
        risk_str = cfg.get("risk", "medium")
        reason = cfg.get("reason", f"Custom rule: {name}")

        risk = RiskLevel(risk_str)
        checker = _make_checker(match)

        rules.append(Rule(name=name, check=checker, risk=risk, reason=reason))
    return rules


def parse_config(config: dict[str, Any]) -> dict[str, Any]:
    """Parse a full config dict into shield kwargs + extra rules.

    Returns a dict with keys: budget, mode, auto_snapshot, max_snapshots, extra_rules, db_path.
    """
    return {
        "budget": config.get("budget"),
        "mode": config.get("mode", "production"),
        "auto_snapshot": config.get("auto_snapshot", True),
        "max_snapshots": config.get("max_snapshots", 1000),
        "extra_rules": rules_from_config(config.get("rules", [])),
        "db_path": config.get("db_path", "warderbox.db"),
    }
