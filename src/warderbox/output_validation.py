"""Output validation for agent tool results.

Filters, redacts, or blocks tool outputs before they flow back to
the agent, preventing sensitive data leaks and enforcing output
contracts.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OutputAction(Enum):
    PASS = 0
    REDACT = 1
    BLOCK = 2


@dataclass
class OutputRule:
    """A single output validation rule."""

    name: str
    tools: set[str] | None = None
    pattern: str | None = None
    max_length: int | None = None
    validator: Callable[[Any], bool] | None = None
    action: OutputAction = OutputAction.BLOCK
    replacement: str = "[REDACTED]"


@dataclass
class OutputCheckResult:
    """Result of validating tool output."""

    passed: bool
    action: OutputAction = OutputAction.PASS
    original_output: Any = None
    filtered_output: Any = None
    violations: list[str] = field(default_factory=list)


class OutputValidator:
    """Validates and filters tool outputs before returning to the agent.

    Usage:
        validator = OutputValidator()
        validator.add_rule(OutputRule(
            name="no_api_keys",
            pattern=r"sk-[a-zA-Z0-9]{20,}",
            action=OutputAction.REDACT,
        ))
        validator.add_rule(OutputRule(
            name="max_response_size",
            max_length=10000,
            action=OutputAction.BLOCK,
        ))

        result = validator.validate("send_email", output)
        if not result.passed:
            # handle blocked output
    """

    def __init__(self) -> None:
        self._rules: list[OutputRule] = []
        self._lock = threading.Lock()
        self._violations_count = 0
        self._checks_count = 0

    def add_rule(self, rule: OutputRule) -> None:
        with self._lock:
            self._rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.name != name]
            return len(self._rules) < before

    def validate(self, tool_name: str, output: Any) -> OutputCheckResult:
        """Validate a tool's output against all applicable rules."""
        with self._lock:
            rules = list(self._rules)

        self._checks_count += 1
        violations: list[str] = []
        final_output = output
        worst_action = OutputAction.PASS

        for rule in rules:
            if rule.tools and tool_name not in rule.tools:
                continue

            output_str = str(final_output) if final_output is not None else ""

            if rule.pattern:
                if re.search(rule.pattern, output_str):
                    violations.append(f"{rule.name}: pattern match '{rule.pattern}'")
                    if rule.action == OutputAction.REDACT:
                        final_output = re.sub(rule.pattern, rule.replacement, output_str)
                    if rule.action.value >= worst_action.value:
                        worst_action = rule.action

            if rule.max_length and len(output_str) > rule.max_length:
                violations.append(
                    f"{rule.name}: output length {len(output_str)} > {rule.max_length}"
                )
                if rule.action == OutputAction.REDACT:
                    final_output = output_str[:rule.max_length] + "...[truncated]"
                if rule.action.value >= worst_action.value:
                    worst_action = rule.action

            if rule.validator and not rule.validator(final_output):
                violations.append(f"{rule.name}: custom validator failed")
                if rule.action.value >= worst_action.value:
                    worst_action = rule.action

        if violations:
            self._violations_count += len(violations)

        passed = worst_action != OutputAction.BLOCK
        if not passed:
            final_output = None

        return OutputCheckResult(
            passed=passed,
            action=worst_action,
            original_output=output,
            filtered_output=final_output,
            violations=violations,
        )

    def add_pii_rules(self) -> None:
        """Add common PII-detection rules for output filtering."""
        patterns = [
            ("output_ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
            ("output_credit_card", r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
            ("output_api_key", r"\b(sk|pk|api|key|token)[_-][a-zA-Z0-9]{20,}\b"),
            ("output_email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        ]
        for name, pattern in patterns:
            self.add_rule(OutputRule(
                name=name,
                pattern=pattern,
                action=OutputAction.REDACT,
            ))

    def stats(self) -> dict:
        return {
            "rules_count": len(self._rules),
            "checks_performed": self._checks_count,
            "violations_found": self._violations_count,
        }
