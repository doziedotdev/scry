"""Best-effort transcript redaction.

This removes common credential shapes before indexing and display, but it is not a
security boundary. Transcript indexes remain sensitive and must be kept private.

Speed matters here: every indexed character passes through this module, and on a
large machine that is gigabytes. The design is a cheap gate, then targeted scans:

1. ``needs_redaction`` lowercases the text once and looks for a handful of substrings
   with ``str.__contains__`` (a tuned C loop, ~180 MB/s). Most turns fail it.
2. Only texts that pass get the identifier scan, which walks keyword hits and reads
   the ``= value`` / ``: value`` after each, and the four shape regexes.

No regex alternation runs over clean text, which is what made the previous version
the largest single cost of an index build.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

# Substrings any redactable text must contain, lowercased. Kept short and specific:
# ``pass`` covers password/passwd/passphrase/DBPASS; ``key`` covers api_key, api-key,
# apikey, private_key, access_key.
NEEDLES = ("secret", "token", "pass", "key", "session", "jwt", "bearer", "sk-", "ghp_", "xoxb-", "akia", "-----begin")

# Keywords that mark an identifier as secret-bearing. ``session`` and ``pass`` only
# count for ``=`` assignments: ``session_id: 01a0…`` in prose is a ref, not a secret.
# ``pass`` matches inside identifiers (DBPASS, PASSPHRASE) except the English compounds
# below, so ``bypass=1`` stays readable while ``MYPASS=hunter2`` is closed.
KEYWORD_RE = re.compile(
    r"(?i)secret|token|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key|jwt|pass|session"
)
ASSIGN_ONLY = ("pass", "session")
PASS_COMPOUND_RE = re.compile(r"(?i)^(?:(?:by|com|sur|tres|over|under|en)pass(?:ed|es|ing|age|ages|enger|ive|port|able|er|ers)?|pass(?:ed|es|ing|age|ages|enger|ive|port|able|er|ers))$")
IDENT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
VALUE_STOP = frozenset(" \t\r\n,}\"'")

BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
PROVIDER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{12,})(?![A-Za-z0-9])"
)
PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:access[_-]?key|api[_-]?key|auth[_-]?token|bearer[_-]?token|jwt|password|passwd|secret|session|token)=)([^&#\s]+)"
)


def needs_redaction(text: str) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in NEEDLES)


def _redact_identifiers(text: str) -> str:
    out: list[str] = []
    last = 0
    n = len(text)
    for match in KEYWORD_RE.finditer(text):
        start, end = match.span()
        if start < last:
            continue
        while end < n and text[end] in IDENT:
            end += 1
        j = end
        if j < n and text[j] in "\"'":
            j += 1
        while j < n and text[j] in " \t":
            j += 1
        if j >= n or text[j] not in "=:":
            continue
        keyword = match.group().lower()
        if text[j] == ":" and keyword in ASSIGN_ONLY:
            continue
        if keyword == "pass":
            ident_start = start
            while ident_start > 0 and text[ident_start - 1] in IDENT:
                ident_start -= 1
            if PASS_COMPOUND_RE.match(text[ident_start:end].split(".")[-1].split("-")[-1]):
                continue
        j += 1
        while j < n and text[j] in " \t":
            j += 1
        if j < n and text[j] in "\"'":
            j += 1
        value_start = j
        while j < n and text[j] not in VALUE_STOP:
            j += 1
        if j == value_start or text[value_start:j] == REDACTED:
            continue
        out.append(text[last:value_start])
        out.append(REDACTED)
        last = j
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def redact(text: str, *, enabled: bool = True) -> str:
    """Return text with recognizable credentials replaced, unless disabled."""
    if not enabled or not text or not needs_redaction(text):
        return text
    lowered = text.lower()
    if "-----begin" in lowered:
        text = PEM_PRIVATE_KEY_RE.sub(REDACTED, text)
    text = _redact_identifiers(text)
    if "bearer" in lowered:
        text = BEARER_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    if "?" in text or "&" in text:
        text = URL_SECRET_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    if "sk-" in lowered or "ghp_" in lowered or "xoxb-" in lowered or "akia" in lowered:
        text = PROVIDER_TOKEN_RE.sub(REDACTED, text)
    return text
