"""Sensitive data masking for audit logs and webhook payloads.

Automatically detects and masks PII, secrets, and credentials in
action arguments before they are logged or sent to webhooks.

Built-in patterns:
- Email addresses
- API keys / tokens (Bearer, OpenAI/Anthropic sk-, Stripe, GitHub, GitLab,
  Slack, Google, Hugging Face, npm, SendGrid, AWS, api_key=..., etc.)
- Private keys (PEM blocks) and credentials embedded in URLs
- Credit card numbers (issuer + Luhn checks; timestamps are left alone)
- Phone numbers, including international +CC and 0-prefixed forms
- SSN-like patterns
- IP addresses
- JWT tokens

Masking is pattern based and therefore best effort: a secret in a format no
pattern knows is only caught when it sits under a sensitive key.

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
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any


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


def _luhn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _is_card_number(digits: str) -> bool:
    """Decide whether a digit run is plausibly a payment card.

    Long numeric ids and timestamps are everywhere in tool output, so masking
    every long number destroys logs. The rules, by length:

    * 13-15 digits: known issuer prefix (2-6) and a valid Luhn check. Epoch
      millisecond timestamps start with 1 and are never masked.
    * 16 digits: masked unless the value is in the epoch-microsecond range.
      Luhn is deliberately not required: failing open on a mistyped or partial
      card is worse than masking an arbitrary 16-digit id.
    * 17-19 digits: Luhn required, and the epoch-nanosecond range at 19 digits
      is excluded (snowflake ids and timestamps dominate these lengths).
    """
    length = len(digits)
    if not 13 <= length <= 19:
        return False
    if length < 16:
        return digits[0] in "23456" and _luhn_valid(digits)
    if length == 16:
        return not 15 <= int(digits[:2]) <= 19
    if length == 19 and 15 <= int(digits[:2]) <= 19:
        return False
    return _luhn_valid(digits)


def _mask_card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if not _is_card_number(digits):
        return match.group(0)
    return f"****-****-****-{digits[-4:]}"


def _mask_phone(match: re.Match) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    # A bare 10-digit number starting with 0 or 1 is not a NANP phone number;
    # it is far more often an epoch-seconds timestamp or an id.
    if raw.isdigit() and len(raw) == 10 and raw[0] in "01":
        return raw
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "***-***-****"


# Tool output can be megabytes of attacker-influenced text, and an unbounded run
# tried from each start position takes quadratic time. So a run is matched
# possessively and whole, starting only where the run starts: the secret is
# never cut off at an arbitrary length, and matching stays linear.
DEFAULT_PATTERNS: list[MaskPattern] = [
    MaskPattern(
        name="jwt_token",
        pattern=re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]++\.eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++"),
        replacement="[JWT_REDACTED]",
    ),
    MaskPattern(
        name="bearer_token",
        pattern=re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]++", re.IGNORECASE),
        replacement="Bearer [REDACTED]",
    ),
    MaskPattern(
        name="private_key",
        pattern=re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
        replacement="[PRIVATE_KEY_REDACTED]",
    ),
    MaskPattern(
        name="url_credentials",
        pattern=re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{0,30}://)[^/\s:@]{0,512}:[^/\s@]{1,1024}@"),
        replacement=r"\1[CREDENTIALS_REDACTED]@",
    ),
    MaskPattern(
        name="basic_auth",
        pattern=re.compile(r"(Authorization\s*:\s*Basic\s+)[A-Za-z0-9+/=]{1,4096}", re.IGNORECASE),
        replacement=r"\1[REDACTED]",
    ),
    MaskPattern(
        name="api_key_openai",
        pattern=re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}+"),
        replacement="sk-[REDACTED]",
    ),
    MaskPattern(
        name="api_key_stripe",
        pattern=re.compile(r"\b(?:[srp]k|whsec)_(?:(?:live|test)_)?[0-9A-Za-z]{16,}+"),
        replacement="[STRIPE_KEY_REDACTED]",
    ),
    MaskPattern(
        name="github_token",
        pattern=re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}+|github_pat_[A-Za-z0-9_]{20,}+)"),
        replacement="[GITHUB_TOKEN_REDACTED]",
    ),
    MaskPattern(
        name="gitlab_token",
        pattern=re.compile(r"\bglpat-[0-9A-Za-z_-]{16,}+"),
        replacement="[GITLAB_TOKEN_REDACTED]",
    ),
    MaskPattern(
        name="slack_token",
        pattern=re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}+|\bxapp-[A-Za-z0-9-]{10,}+"),
        replacement="[SLACK_TOKEN_REDACTED]",
    ),
    MaskPattern(
        name="google_api_key",
        pattern=re.compile(r"\bAIza[0-9A-Za-z_-]{30,}+"),
        replacement="[GOOGLE_KEY_REDACTED]",
    ),
    MaskPattern(
        name="huggingface_token",
        pattern=re.compile(r"\bhf_[A-Za-z0-9]{20,}+"),
        replacement="[HF_TOKEN_REDACTED]",
    ),
    MaskPattern(
        name="npm_token",
        pattern=re.compile(r"\bnpm_[A-Za-z0-9]{20,}+"),
        replacement="[NPM_TOKEN_REDACTED]",
    ),
    MaskPattern(
        name="sendgrid_key",
        pattern=re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}+\.[A-Za-z0-9_-]{16,}+"),
        replacement="[SENDGRID_KEY_REDACTED]",
    ),
    MaskPattern(
        name="aws_key",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}"),
        replacement="AKIA[REDACTED]",
    ),
    MaskPattern(
        name="aws_session_key",
        pattern=re.compile(r"\bASIA[0-9A-Z]{16}"),
        replacement="ASIA[REDACTED]",
    ),
    MaskPattern(
        name="password_assignment",
        pattern=re.compile(
            r"\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|client[_-]?secret|auth[_-]?token)"
            r"(\s*[=:]\s*)['\"]?(?!\[)[^\s'\",;&]++", re.IGNORECASE
        ),
        replacement=r"\1\2[REDACTED]",
    ),
    MaskPattern(
        name="api_key_generic",
        pattern=re.compile(r"(?:api[_-]?key|token|secret|password|auth)\s*[=:]\s*['\"]?([A-Za-z0-9_\-]{16,}+)", re.IGNORECASE),
        replacement="[API_KEY_REDACTED]",
    ),
    MaskPattern(
        name="iban",
        pattern=re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
        replacement="[IBAN_REDACTED]",
    ),
    MaskPattern(
        name="credit_card",
        pattern=re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        mask_fn=_mask_card,
    ),
    MaskPattern(
        name="email",
        pattern=re.compile(r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]++@[a-zA-Z0-9.-]{1,255}\.[a-zA-Z]{2,63}"),
        mask_fn=_mask_email,
    ),
    MaskPattern(
        name="ssn",
        pattern=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        replacement="***-**-****",
    ),
    MaskPattern(
        name="phone",
        pattern=re.compile(
            r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
        ),
        mask_fn=_mask_phone,
    ),
    MaskPattern(
        name="phone_international",
        pattern=re.compile(
            r"(?<![\w+])(?:\+\d{1,3}[-.\s]?|0)\(?\d{2,4}\)?(?:[-.\s]?\d){6,10}(?!\w)"
        ),
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

    def mask(self, data: Any) -> Any:
        """Return a masked copy of *data*.

        Handles every shape a tool call can carry: strings, bytes, numbers,
        mappings (keys included), lists, tuples, sets and arbitrary objects
        (via ``str()``, which is what the audit log would otherwise store).
        """
        if not self._enabled:
            return data
        return self._mask_value(data, 0, set())

    def _mask_value(self, data: Any, depth: int, active: set[int]) -> Any:
        if isinstance(data, str):
            return self.mask_string(data)
        if data is None or isinstance(data, bool):
            return data
        if isinstance(data, (int, float)):
            return self._mask_scalar(data)
        if isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
            text = raw.decode("utf-8", "replace")
            masked = self.mask_string(text)
            return data if masked == text else masked

        if depth >= self.MAX_DEPTH:
            return "[MAX_DEPTH_REDACTED]"
        marker = id(data)
        if marker in active:
            return "[CYCLE_REDACTED]"
        active.add(marker)
        try:
            if isinstance(data, Mapping):
                return {
                    self.mask_string(k) if isinstance(k, str) else k:
                    self._mask_item(k, v, depth, active)
                    for k, v in data.items()
                }
            if isinstance(data, list):
                return [self._mask_value(v, depth + 1, active) for v in data]
            if isinstance(data, tuple):
                return tuple(self._mask_value(v, depth + 1, active) for v in data)
            if isinstance(data, (set, frozenset)):
                items = [self._mask_value(v, depth + 1, active) for v in data]
                try:
                    return type(data)(items)
                except TypeError:
                    return items
        finally:
            active.discard(marker)

        # Arbitrary object: only str() can reveal what it would log as. Keep
        # the object when nothing sensitive shows; fail closed if str() raises.
        try:
            text = str(data)
        except Exception:
            return "[UNMASKABLE_OBJECT]"
        masked = self.mask_string(text)
        return data if masked == text else masked

    def _mask_item(self, key: Any, value: Any, depth: int, active: set[int]) -> Any:
        if self._is_sensitive_key(key):
            # Everything under a sensitive key is sensitive, however nested.
            if value is None or value == "":
                return value
            if isinstance(value, str):
                masked = self.mask_string(value)
                return masked if masked != value else "[REDACTED]"
            return "[REDACTED]"
        return self._mask_value(value, depth + 1, active)

    MAX_DEPTH = 32

    # Matched against whole words of the key, so "ping", "mapping" and
    # "max_tokens" are not treated as "pin" and "token".
    _SENSITIVE_WORDS = frozenset({
        "ssn", "password", "passwd", "pwd", "passphrase", "secret", "secrets",
        "token", "apikey", "cardnumber", "cvv", "cvc", "pin", "credential",
        "credentials", "authorization", "cookie", "session", "iban", "tckn",
        "phone",
    })
    _SENSITIVE_PAIRS = frozenset({
        ("social", "security"), ("api", "key"), ("credit", "card"),
        ("card", "number"), ("private", "key"), ("access", "key"),
        ("access", "tokens"), ("refresh", "tokens"), ("auth", "tokens"),
        ("api", "tokens"),
    })
    # "token" next to one of these is LLM usage accounting, not a credential.
    _TOKEN_USAGE_WORDS = frozenset({
        "count", "counts", "limit", "limits", "max", "min", "total", "usage",
        "used", "budget", "input", "output", "prompt", "completion", "num",
        "cost", "per",
    })

    def _is_sensitive_key(self, key: Any) -> bool:
        if not isinstance(key, str):
            return False
        words = [
            w.lower()
            for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", key)
        ]
        hit = next((w for w in words if w in self._SENSITIVE_WORDS), None)
        if hit == "token" and self._TOKEN_USAGE_WORDS.intersection(words):
            hit = None
        return hit is not None or any(
            pair in self._SENSITIVE_PAIRS for pair in pairwise(words)
        )

    def _mask_scalar(self, value):
        """Mask numeric PII (e.g. a card number passed as an int) without
        changing the value when it isn't sensitive."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        text = str(value)
        masked = self.mask_string(text)
        return masked if masked != text else value

    def detect(self, text: str) -> list[str]:
        """Detect which patterns match in the given text (without masking)."""
        if not isinstance(text, str):
            return []
        found = []
        for p in self._patterns:
            for match in p.pattern.finditer(text):
                # mask_fn patterns may decline a match (e.g. a timestamp).
                if p.mask_fn is None or p.mask_fn(match) != match.group(0):
                    found.append(p.name)
                    break
        return found
