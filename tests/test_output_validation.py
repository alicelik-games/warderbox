"""Tests for output validation."""

from warderbox.output_validation import (
    OutputAction,
    OutputRule,
    OutputValidator,
)


def test_pass_without_rules():
    v = OutputValidator()
    result = v.validate("tool_a", "hello world")
    assert result.passed is True
    assert result.filtered_output == "hello world"


def test_pattern_block():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="no_secrets",
        pattern=r"sk-[a-zA-Z0-9]{20,}",
        action=OutputAction.BLOCK,
    ))
    result = v.validate("tool", "key is sk-abcdefghijklmnopqrstuvwxyz")
    assert result.passed is False
    assert result.filtered_output is None


def test_pattern_redact():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="redact_keys",
        pattern=r"sk-[a-zA-Z0-9]{20,}",
        action=OutputAction.REDACT,
    ))
    result = v.validate("tool", "key is sk-abcdefghijklmnopqrstuvwxyz")
    assert result.passed is True
    assert "sk-" not in result.filtered_output
    assert "[REDACTED]" in result.filtered_output


def test_custom_replacement():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="hide_ssn",
        pattern=r"\d{3}-\d{2}-\d{4}",
        action=OutputAction.REDACT,
        replacement="***-**-****",
    ))
    result = v.validate("tool", "SSN: 123-45-6789")
    assert "***-**-****" in result.filtered_output


def test_max_length_block():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="size_limit",
        max_length=10,
        action=OutputAction.BLOCK,
    ))
    result = v.validate("tool", "a" * 100)
    assert result.passed is False


def test_max_length_redact():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="truncate",
        max_length=10,
        action=OutputAction.REDACT,
    ))
    result = v.validate("tool", "a" * 100)
    assert result.passed is True
    assert len(result.filtered_output) < 100
    assert "truncated" in result.filtered_output


def test_tool_scoping():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="email_only",
        tools={"send_email"},
        pattern=r"secret",
        action=OutputAction.BLOCK,
    ))
    r1 = v.validate("send_email", "this is secret")
    assert r1.passed is False
    r2 = v.validate("other_tool", "this is secret")
    assert r2.passed is True


def test_custom_validator():
    v = OutputValidator()
    v.add_rule(OutputRule(
        name="must_be_dict",
        validator=lambda x: isinstance(x, dict),
        action=OutputAction.BLOCK,
    ))
    assert v.validate("tool", {"ok": True}).passed is True
    assert v.validate("tool", "not a dict").passed is False


def test_remove_rule():
    v = OutputValidator()
    v.add_rule(OutputRule(name="r1", pattern=r"bad", action=OutputAction.BLOCK))
    assert v.validate("t", "bad stuff").passed is False
    assert v.remove_rule("r1") is True
    assert v.validate("t", "bad stuff").passed is True


def test_remove_nonexistent():
    v = OutputValidator()
    assert v.remove_rule("nope") is False


def test_multiple_rules():
    v = OutputValidator()
    v.add_rule(OutputRule(name="r1", pattern=r"ssn:\d+", action=OutputAction.REDACT))
    v.add_rule(OutputRule(name="r2", pattern=r"secret", action=OutputAction.BLOCK))
    result = v.validate("tool", "ssn:123 and secret data")
    assert result.passed is False
    assert len(result.violations) == 2


def test_pii_rules():
    v = OutputValidator()
    v.add_pii_rules()
    result = v.validate("tool", "SSN is 123-45-6789")
    assert result.passed is True
    assert "123-45-6789" not in result.filtered_output


def test_stats():
    v = OutputValidator()
    v.add_rule(OutputRule(name="r1", pattern=r"bad", action=OutputAction.BLOCK))
    v.validate("t", "good")
    v.validate("t", "bad")
    stats = v.stats()
    assert stats["rules_count"] == 1
    assert stats["checks_performed"] == 2
    assert stats["violations_found"] == 1


def test_none_output():
    v = OutputValidator()
    v.add_rule(OutputRule(name="r1", pattern=r"x", action=OutputAction.BLOCK))
    result = v.validate("tool", None)
    assert result.passed is True


def test_violations_list():
    v = OutputValidator()
    v.add_rule(OutputRule(name="check1", pattern=r"\d{3}", action=OutputAction.REDACT))
    result = v.validate("tool", "code 123")
    assert len(result.violations) == 1
    assert "check1" in result.violations[0]
