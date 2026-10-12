"""Redaction closes the common credential shapes and leaves ordinary transcript text alone."""

from __future__ import annotations

import pytest

from scry.redact import REDACTED, redact


@pytest.mark.parametrize(
    "text, secret",
    [
        ("API_TOKEN=assignment-secret", "assignment-secret"),
        ('{"password": "json-secret"}', "json-secret"),
        ("access_key: yaml-secret", "yaml-secret"),
        ("DB_PASS=hunter2", "hunter2"),
        ("DBPASS=hunter3", "hunter3"),
        ("PASSPHRASE=correct-horse", "correct-horse"),
        ("pass=plain-value", "plain-value"),
        ("export SESSION=abc123", "abc123"),
        ("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
        ("leaked sk-proj-abcdefghijklmnopqrstuvwxyz012345", "sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
        ("leaked ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        # Built from parts so secret scanners do not flag this synthetic token.
        ("leaked " + "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuvwx", "xox" + "b-123456789012"),
        ("leaked AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("https://example.test/cb?next=home&access_token=query-param-secret&ok=1", "query-param-secret"),
        ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj\n-----END PRIVATE KEY-----", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj"),
    ],
)
def test_credential_shapes_are_closed(text, secret):
    rendered = redact(text)
    assert REDACTED in rendered and secret not in rendered


@pytest.mark.parametrize(
    "text",
    [
        "session_id: 01a07243-c103-711b-b1cc-56d99fee43f4",  # a ref, not a secret
        "the bypass=1 flag and passed=true and compass=north",
        "token count 4,096 for this turn",
        "client credential 550e8400-e29b-41d4-a716-446655440000",
    ],
)
def test_ordinary_text_is_untouched(text):
    assert redact(text) == text


def test_disabled_and_idempotent():
    text = "API_TOKEN=visible-by-explicit-request"
    assert redact(text, enabled=False) == text
    once = redact("Authorization: Bearer abcdefghijklmnop1234")
    assert redact(once) == once
