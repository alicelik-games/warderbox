"""Policy-as-code: load shield configuration from YAML or JSON files.

Supports both JSON (built-in) and YAML (optional pyyaml dependency).

Usage:
    shield = load_policy("policy.yaml")
    # or
    shield = load_policy("policy.json")

Example policy.yaml:
    budget: 10.00
    mode: production
    db_path: audit.db
    global_rpm: 60
    tool_limits:
      send_email:
        rpm: 5
        rph: 20
    rules:
      - name: no_prod_access
        match:
          args_regex: "production|prod-db"
        risk: critical
        reason: Production database access blocked
    webhooks:
      - url: https://hooks.slack.com/services/xxx
        events: [action_blocked, budget_exceeded]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warderbox.shield import WarderBox


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "PyYAML is required for YAML policy files. "
            "Install it with: pip install warderbox[yaml]"
        )
    with open(path) as f:
        return yaml.safe_load(f)


def _load_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def load_policy(path: str | Path) -> WarderBox:
    """Load an WarderBox instance from a policy file.

    Supports .yaml, .yml, and .json files.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Policy file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        config = _load_yaml(path)
    elif suffix == ".json":
        config = _load_json(path)
    else:
        raise ValueError(f"Unsupported policy file format: {suffix} (use .yaml, .yml, or .json)")

    if not isinstance(config, dict):
        raise ValueError(f"Policy file must contain a mapping, got {type(config).__name__}")

    issues = _validate_config(config)
    if issues:
        raise ValueError(
            "Invalid policy file:\n  - " + "\n  - ".join(issues)
        )

    return WarderBox.from_config(config)


def validate_policy(path: str | Path) -> list[str]:
    """Validate a policy file and return a list of warnings/errors."""
    path = Path(path)

    if not path.exists():
        return [f"File not found: {path}"]

    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            config = _load_yaml(path)
        elif suffix == ".json":
            config = _load_json(path)
        else:
            return [f"Unsupported format: {suffix}"]
    except Exception as e:
        return [f"Parse error: {e}"]

    if not isinstance(config, dict):
        return ["Policy must be a dict/mapping"]

    return _validate_config(config)


def _validate_config(config: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    if "budget" in config and (
        isinstance(config["budget"], bool)
        or not isinstance(config["budget"], (int, float))
    ):
        issues.append(f"budget should be a number, got {type(config['budget']).__name__}")

    if "mode" in config and config["mode"] not in ("production", "test"):
        issues.append(f"mode must be 'production' or 'test', got '{config['mode']}'")

    valid_risk = {"low", "medium", "high", "critical"}
    for i, rule in enumerate(config.get("rules", [])):
        if not isinstance(rule, dict):
            issues.append(f"rules[{i}]: must be a dict")
            continue
        if "match" not in rule:
            issues.append(f"rules[{i}]: missing 'match' field")
        risk = rule.get("risk", "medium")
        if risk not in valid_risk:
            issues.append(f"rules[{i}]: invalid risk '{risk}' (use: {valid_risk})")

    for wh in config.get("webhooks", []):
        if "url" not in wh:
            issues.append("webhook entry missing 'url'")

    if "global_rpm" in config and (
        isinstance(config["global_rpm"], bool)
        or not isinstance(config["global_rpm"], int)
    ):
        issues.append("global_rpm should be an integer")

    if "global_rph" in config and not isinstance(config["global_rph"], int):
        issues.append("global_rph should be an integer")

    return issues
