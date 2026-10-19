"""Sensitive data masking for audit logs and webhook payloads.

Automatically detects and masks PII, secrets, and credentials in
action arguments before they are logged or sent to webhooks.

Built-in patterns:
- Email addresses
- API keys / tokens (Bearer, sk-, api_key, etc.)
- Credit card numbers (13-19 digits)
- Phone numbers
- SSN-like patterns
- IP addresses
- JWT tokens
- AWS keys

Usage:
    masker = DataMasker()
    masked = masker.mask({"email": "user@example.com", "query": "SELECT * FROM users"})
    # {"email": "u***@***.com", "query": "SELECT * FROM users"}

    # Add custom patterns
    masker.add_pattern("internal_id", r"PROJ-\\d{6}", "PROJ-******")

    # Use with WarderBox
    shield = WarderBox(budget=5.0)
    shield.set_masker(masker)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MaskPattern:
    name: str
    pattern: re.Pattern
    replacement: str | None = None
    mask_fn: callable | None = None


def _mask_email(match: re.Match) -> str:
    email = match.group(0)
    local, domain = email.split("@", 1)
    parts = domain.split(".")
    return f"{local[0]}***@***.{parts[-1]}"


def _mask_card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return f"****-****-****-{digits[-4:]}"


def _mask_phone(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "***-***-****"


DEFAULT_PATTERNS: list[MaskPattern] = [
    MaskPattern(
        name="jwt_token",
        pattern=re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        replacement="[JWT_REDACTED]",
    ),
    MaskPattern(
        name="bearer_token",
        pattern=re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
        replacement="Bearer [REDACTED]",
    ),
    MaskPattern(
        name="api_key_openai",
        pattern=re.compile(r"sk-[A-Za-z0-9]{20,}"),
        replacement="sk-[REDACTED]",
    ),
    MaskPattern(
        name="aws_key",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        replacement="AKIA[REDACTED]",
    ),
    MaskPattern(
        name="api_key_generic",
        pattern=re.compile(r"(?:api[_-]?key|token|secret|password|auth)\s*[=:]\s*['\"]?([A-Za-z0-9_\-]{16,})", re.IGNORECASE),
        replacement="[API_KEY_REDACTED]",
    ),
    MaskPattern(
        name="credit_card",
        pattern=re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        mask_fn=_mask_card,
    ),
    MaskPattern(
        name="email",
        pattern=re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        mask_fn=_mask_email,
    ),
    MaskPattern(
        name="ssn",
        pattern=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        replacement="***-**-****",
    ),
    MaskPattern(
        name="phone",
        pattern=re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        mask_fn=_mask_phone,
    ),
    MaskPattern(
        name="ipv4",
        pattern=re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        replacement="***.***.***.***",
    ),
]


class DataMasker:
    """Masks sensitive data in strings and dicts."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._patterns: list[MaskPattern] = list(DEFAULT_PATTERNS)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def add_pattern(
        self,
        name: str,
        pattern: str,
        replacement: str | None = None,
        mask_fn: callable | None = None,
        flags: int = 0,
    ) -> None:
        self._patterns.append(MaskPattern(
            name=name,
            pattern=re.compile(pattern, flags),
            replacement=replacement,
            mask_fn=mask_fn,
        ))

    def mask_string(self, text: str) -> str:
        if not self._enabled or not isinstance(text, str):
            return text

        for p in self._patterns:
            if p.mask_fn:
                text = p.pattern.sub(p.mask_fn, text)
            elif p.replacement:
                text = p.pattern.sub(p.replacement, text)

        return text

    def mask(self, data: dict | str | list) -> dict | str | list:
        if not self._enabled:
            return data

        if isinstance(data, str):
            return self.mask_string(data)

        if isinstance(data, list):
            return [self.mask(item) for item in data]

        if isinstance(data, dict):
            masked = {}
            for key, value in data.items():
                if isinstance(value, str):
                    masked[key] = self.mask_string(value)
                elif isinstance(value, (dict, list)):
                    masked[key] = self.mask(value)
                else:
                    masked[key] = value
            return masked

        return data

    def detect(self, text: str) -> list[str]:
        """Detect which patterns match in the given text (without masking)."""
        if not isinstance(text, str):
            return []
        found = []
        for p in self._patterns:
            if p.pattern.search(text):
                found.append(p.name)
        return found
