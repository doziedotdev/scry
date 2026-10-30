"""One line per turn, computed the same way for every harness.

The gist is the unit of walking: a 500 turn session walks in 40 lines because each
turn costs one gist. Gists are computed at index time from the normalized turn, so
adapters never invent their own summaries.
"""

from __future__ import annotations

import json
import re

from .model import Call, Turn

GIST_CHARS = 110

_WS_RE = re.compile(r"\s+")
_HEAD_RE = re.compile(r"^(?:#{1,6}\s+|[-*]\s+|>\s+|\d+\.\s+)")
_PATH_RE = re.compile(r"(?<![\w/])(?:~|/)?(?:[\w.@-]+/)+[\w.@-]+(?:\.[\w-]+)?|(?<![\w/])[\w.@-]+\.(?:py|ts|js|tsx|jsx|md|json|toml|yaml|yml|sh|sql|html|css|go|rs|txt|csv|jsonl|log)\b")
_FREE_TEXT_KEYS = frozenset({"content", "code", "text", "message", "prompt", "input", "body", "task", "context", "description", "i", "intent", "q", "query", "pattern"})
_ARG_KEYS = ("i", "intent", "path", "Path", "file", "paths", "pattern", "command", "CommandLine", "cmd", "query", "q", "url", "title", "name", "description", "task", "message", "Message", "prompt", "Prompt", "code", "content", "Content")


def squash(text: str, limit: int = GIST_CHARS) -> str:
    """First meaningful line, whitespace collapsed, cut to ``limit`` with an ellipsis."""
    for line in text.splitlines():
        stripped = _HEAD_RE.sub("", line.strip())
        if stripped:
            stripped = _WS_RE.sub(" ", stripped)
            return stripped if len(stripped) <= limit else stripped[: limit - 1].rstrip() + "…"
    return ""


def task_gist(text: str, limit: int = 200) -> str:
    """The first substantive line of a delegated task.

    Harness wrappers put a label line first ("Complete assignment thoroughly:") and
    task templates start with headings ("# Target"); neither says what the task is.
    """
    fallback = ""
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if not fallback:
            fallback = raw
        if raw.startswith("#") or (raw.endswith(":") and len(raw) < 48):
            continue
        return squash(raw, limit)
    return squash(fallback, limit)


def call_gist(call: Call, limit: int = 80) -> str:
    """``name: intent`` when the harness gave one, otherwise ``name key=value`` from the args."""
    if call.intent:
        return squash(f"{call.name}: {call.intent}", limit)
    args = _parse_args(call.args)
    if isinstance(args, dict):
        for key in _ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return squash(f"{call.name} {key}={value}", limit)
            if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                return squash(f"{call.name} {key}={' '.join(value)}", limit)
        return squash(f"{call.name} {' '.join(str(k) for k in list(args)[:4])}", limit)
    if isinstance(args, str) and args.strip():
        return squash(f"{call.name} {args}", limit)
    return call.name


def turn_gist(turn: Turn) -> str:
    if turn.speaker == "tool":
        name = turn.tool or "tool"
        if turn.is_error:
            return squash(f"{name} ERROR {squash(turn.text, 70)}", GIST_CHARS)
        return f"{name} ok {human_size(len(turn.text))}"
    text = squash(turn.text)
    if text:
        if turn.calls:
            calls = " · ".join(call_gist(call, 50) for call in turn.calls[:2])
            return squash(f"{text} → {calls}", GIST_CHARS + 40)
        return text
    if turn.calls:
        return "→ " + " · ".join(call_gist(call, 60) for call in turn.calls[:3])
    if turn.thinking:
        return "(thinking) " + squash(turn.thinking, GIST_CHARS - 11)
    return "(empty)"


def call_paths(call: Call, *, explicit_only: bool = False) -> tuple[str, ...]:
    """File paths named in a tool call's arguments, deduplicated in order.

    ``explicit_only`` reads only the well-known path keys; use it for write tools, whose
    content argument can contain anything that looks like a path.
    """
    args = _parse_args(call.args)
    found: list[str] = []
    if isinstance(args, dict):
        for key in ("path", "Path", "file", "file_path", "filePath", "target", "paths", "files"):
            value = args.get(key)
            if isinstance(value, str):
                found.append(value)
            elif isinstance(value, list):
                found.extend(v for v in value if isinstance(v, str))
        text = "" if explicit_only else " ".join(str(v) for k, v in args.items() if isinstance(v, str) and k not in _FREE_TEXT_KEYS)
    else:
        text = "" if explicit_only else call.args
    for match in _PATH_RE.finditer(text):
        found.append(match.group(0))
    seen: set[str] = set()
    out: list[str] = []
    for item in found:
        item = item.strip().rstrip(".,;:)")
        if len(item) < 3 or item in seen or "://" in item or " " in item or "/" not in item and "." not in item:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out[:20])

def human_size(chars: int) -> str:
    if chars < 1000:
        return f"{chars}c"
    if chars < 100_000:
        return f"{chars / 1000:.1f}k"
    return f"{chars // 1000}k"


def _parse_args(args: str):
    if not args:
        return {}
    if args[:1] in "{[":
        try:
            return json.loads(args)
        except ValueError:
            return args
    return args
