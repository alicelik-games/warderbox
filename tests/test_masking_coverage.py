"""Masking across every data shape, secret format and false-positive trap."""

from __future__ import annotations

import json
import time

import pytest

from warderbox import WarderBox
from warderbox.logger import ActionLogger
from warderbox.masking import DataMasker

masker = DataMasker()
SECRET = "user@example.com"


@pytest.mark.parametrize("value", [
    "ghp_" + "a" * 36,
    "github_pat_" + "a" * 40,
    "xoxb-1234567890-abcdefghij",
    "AIza" + "a" * 35,
    "sk_live_" + "a" * 24,
    "rk_test_" + "a" * 24,
    "glpat-" + "a" * 20,
    "hf_" + "a" * 30,
    "npm_" + "a" * 36,
    "SG." + "a" * 22 + "." + "b" * 43,
    "ASIA" + "A" * 16,
    "AKIA" + "A" * 16,
    "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
])
def test_known_secret_formats_are_removed(value):
    masked = masker.mask_string(f"value {value} end")
    assert value not in masked
    assert masked.endswith("end")


def test_url_credentials_and_short_passwords_are_removed():
    masked = masker.mask_string("postgres://admin:hunter2@db.example.com/prod")
    assert "hunter2" not in masked and "admin" not in masked
    assert "db.example.com" in masked
    assert "hunter2" not in masker.mask_string("password=hunter2")


@pytest.mark.parametrize("data", [
    (SECRET,),
    {SECRET},
    frozenset({SECRET}),
    [(SECRET,)],
    {"a": (SECRET,)},
    SECRET.encode(),
    {SECRET: 1},
])
def test_every_container_is_masked(data):
    assert SECRET not in json.dumps(masker.mask(data), default=str)


def test_objects_are_masked_through_str():
    class Holder:
        def __str__(self):
            return f"contact {SECRET}"

    assert SECRET not in str(masker.mask(Holder()))
    plain = object()
    assert masker.mask(plain) is plain


def test_object_whose_str_raises_is_not_logged_raw():
    class Bad:
        def __str__(self):
            raise RuntimeError("no")

    assert masker.mask(Bad()) == "[UNMASKABLE_OBJECT]"


def test_cycles_and_depth_do_not_crash():
    loop: dict = {}
    loop["self"] = loop
    assert "CYCLE" in json.dumps(masker.mask(loop))
    deep: list = []
    for _ in range(100):
        deep = [deep]
    assert "MAX_DEPTH" in json.dumps(masker.mask(deep))


def test_shapes_are_preserved_when_nothing_is_sensitive():
    assert masker.mask((1, "a")) == (1, "a")
    assert masker.mask(b"plain") == b"plain"
    assert masker.mask({"a", "b"}) == {"a", "b"}


@pytest.mark.parametrize("value", [
    1720000000,  # epoch seconds
    1720000000000,  # epoch milliseconds
    1720000000000000,  # epoch microseconds
    1720000000000000000,  # epoch nanoseconds
])
def test_timestamps_survive(value):
    assert masker.mask(value) == value
    assert masker.mask_string(f"at {value}") == f"at {value}"


def test_no_millisecond_timestamp_is_ever_masked():
    for offset in range(0, 500_000, 7_919):
        ts = str(1_700_000_000_000 + offset)
        assert masker.mask_string(ts) == ts


@pytest.mark.parametrize("card", [
    "4111111111111111",
    "4111 1111 1111 1111",
    "378282246310005",  # Amex, 15 digits
    "1234567812345678",  # 16 digits, not Luhn valid, still treated as a card
])
def test_cards_are_masked(card):
    assert card not in masker.mask_string(card)


@pytest.mark.parametrize("phone", [
    "(415) 555-2671", "4155552671", "+1 415 555 2671", "+90 532 123 45 67",
    "0532 123 45 67",
])
def test_phones_are_masked(phone):
    assert masker.mask_string(phone) != phone


@pytest.mark.parametrize("key", [
    "password", "api_key", "apiKey", "auth_token", "accessToken", "client_secret",
    "card_number", "ssn", "pin", "user_pin", "credentials", "Authorization",
])
def test_sensitive_keys_are_redacted(key):
    assert masker.mask({key: "plainvalue"})[key] == "[REDACTED]"


@pytest.mark.parametrize("key", [
    "ping", "mapping", "shipping", "max_tokens", "total_tokens", "token_count",
    "input_tokens", "secretary", "spinner", "tokenizer",
])
def test_lookalike_keys_are_kept(key):
    assert masker.mask({key: "plainvalue"})[key] == "plainvalue"


def test_nested_values_under_sensitive_key_are_redacted():
    masked = masker.mask({"password": ["a", {"b": "c"}], "token": ("x",)})
    assert masked == {"password": "[REDACTED]", "token": "[REDACTED]"}


def test_detect_ignores_declined_matches():
    assert "credit_card" not in masker.detect("ts 1720000000000")
    assert "credit_card" in masker.detect("card 4111111111111111")


def test_positional_args_never_reach_the_audit_log_raw(tmp_path):
    shield = WarderBox(budget=1.0, db_path=str(tmp_path / "a.db"))

    @shield.tool(cost=0.01)
    def send(*recipients):
        return recipients

    send(SECRET)
    row = ActionLogger(tmp_path / "a.db").query()[0]
    assert SECRET not in row["args"]
    assert SECRET not in row["output"]


@pytest.mark.parametrize("text", [
    "a" * 1_000_000,
    "a." * 500_000,
    "eyJ" * 300_000,
    "sk-" * 300_000,
    "a@" * 500_000,
    "1 " * 500_000,
    "http://a:" * 100_000,
], ids=["alnum", "dots", "jwt", "sk", "at", "digits", "url"])
def test_masking_time_stays_linear_on_hostile_text(text):
    start = time.perf_counter()
    masker.mask_string(text)
    assert time.perf_counter() - start < 10


@pytest.mark.parametrize(("text", "secret"), [
    ("TR330006100519786457841326", "0006100519786457841326"),
    ("TR33 0006 1005 1978 6457 8413 26", "1005 1978"),
    ("DE89 3704 0044 0532 0130 00", "3704 0044"),
    ("a" * 5000 + "@example.com", "a" * 8),
    ("sk-" + "a" * 5000, "a" * 8),
    ("ghp_" + "a" * 5000, "a" * 8),
    ("eyJhbGci.eyJzdWIi." + "a" * 5000, "a" * 8),
    ("Bearer " + "a" * 9000, "a" * 8),
    ("password=" + "a" * 5000, "a" * 8),
    ("api_key=abcd1234", "abcd1234"),
    ("token=abcd", "abcd"),
    ("secret='abc'", "abc'"),
])
def test_secret_is_fully_masked_whatever_its_length(text, secret):
    assert secret not in DataMasker().mask_string(text)


@pytest.mark.parametrize("key", ["session", "phone", "tckn", "iban", "session_id"])
def test_sensitive_personal_keys_are_masked(key):
    assert DataMasker().mask({key: "value"})[key] == "[REDACTED]"


@pytest.mark.parametrize("text", [
    "max_tokens=512",
    "tokens: 100",
    "the token was valid",
    "AB12 is a label",
    "SKU AB1234",
])
def test_ordinary_text_is_left_alone(text):
    assert DataMasker().mask_string(text) == text


@pytest.mark.parametrize("text", [
    "a" * 2_000_000,
    "a@" * 500_000,
    "eyJ-" * 500_000,
    "sk-" * 500_000,
    "password=" * 200_000,
    "AB12 " * 300_000,
])
def test_hostile_input_stays_fast(text):
    start = time.perf_counter()
    DataMasker().mask_string(text)
    assert time.perf_counter() - start < 5
