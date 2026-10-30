"""Output that fits. Every view builds through ``Out`` so the ceiling holds by construction.

An elision never hides where the rest lives: the caller adds a pointer line naming the
ref that recovers it. ``Out.add`` refuses lines that would cross the budget and reports
it, so views can degrade in their fixed order instead of truncating mid-sentence.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

_WS = re.compile(r"\s+")

TRAILER_RESERVE = 240  # room kept for the forced ``more:`` and ``next:`` lines


class Out:
    """Line buffer with a hard ceiling.

    Ordinary lines may use ``budget - TRAILER_RESERVE``; forced lines (the trailer) may
    use the rest and are cut if even that is short. The text never exceeds ``budget``.
    """

    def __init__(self, budget: int) -> None:
        self.budget = max(200, budget)
        self.lines: list[str] = []
        self.used = 0
        self.overflowed = False

    @property
    def room(self) -> int:
        return self.budget - TRAILER_RESERVE - self.used

    def add(self, line: str = "", *, force: bool = False) -> bool:
        cost = len(line) + 1
        force = force or not self.lines  # the header always lands: it says what the reader is looking at
        if force:
            hard = self.budget - self.used
            if cost > hard:
                line = cut_tokens(line, max(0, hard - 1))
                cost = len(line) + 1
                if cost > hard or not line.strip("… "):
                    return False
        elif cost > self.room:
            self.overflowed = True
            return False
        self.lines.append(line)
        self.used += cost
        return True

    def add_block(self, text: str, *, reserve: int = 0) -> bool:
        """Add a multi-line block only if it fits whole, keeping ``reserve`` characters free."""
        cost = len(text) + 1
        if cost > self.room - reserve:
            self.overflowed = True
            return False
        for line in text.split("\n"):
            self.lines.append(line)
        self.used += cost
        return True

    def text(self) -> str:
        return "\n".join(self.lines).rstrip()


def cut(text: str, limit: int) -> str:
    """Cut to ``limit`` characters with an ellipsis, on a word boundary when one is near."""
    text = _WS.sub(" ", text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space > limit * 0.6:
        head = head[:space]
    return head.rstrip() + "…"


def cut_block(text: str, limit: int, more: str) -> str:
    """Keep the first ``limit`` characters of a multi-line text and name the rest."""
    text = (text or "").rstrip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    newline = head.rfind("\n")
    if newline > limit * 0.5:
        head = head[:newline]
    return head.rstrip() + f"\n… [+{len(text) - len(head):,} chars: {more}]"


def cut_tokens(text: str, limit: int) -> str:
    """Cut at a whitespace boundary only, so a ref is either whole or absent.

    Used for the forced trailer lines, which are made of refs: ``next: ref=a · ref=b``
    cut mid-token would print an address that resolves to nothing.
    """
    text = _WS.sub(" ", text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ""
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space <= 0:
        return ""
    head = head[:space].rstrip(" ·")
    return head + " …" if head else ""


def fmt_ts(ts: float | None, *, now: float | None = None) -> str:
    if not ts:
        return "?"
    now = now or time.time()
    dt = datetime.fromtimestamp(ts, timezone.utc)
    if abs(now - ts) < 300 * 86400:
        return dt.strftime("%b %d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_date(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def fmt_clock(ts: float | None) -> str:
    if not ts:
        return "--:--"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def fmt_span(start: float | None, end: float | None, *, now: float | None = None) -> str:
    if not start:
        return fmt_ts(end, now=now)
    if not end or end - start < 60:
        return fmt_ts(start, now=now)
    same_day = fmt_date(start) == fmt_date(end)
    return f"{fmt_ts(start, now=now)}→{fmt_clock(end) if same_day else fmt_ts(end, now=now)}"


def ago(ts: float | None, *, now: float | None = None) -> str:
    if not ts:
        return "unknown"
    delta = max(0.0, (now or time.time()) - ts)
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta // 60)}m ago"
    if delta < 172800:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


def duration(start: float | None, end: float | None) -> str:
    if not start or not end or end <= start:
        return ""
    seconds = end - start
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def short_path(path: str | None, home: str | None = None) -> str:
    if not path:
        return "(no project)"
    home = home or os.path.expanduser("~")
    if path == home:
        return "~"
    norm_path = path.replace("\\", "/")
    norm_home = home.replace("\\", "/").rstrip("/")
    if norm_path.lower() == norm_home.lower():
        return "~"
    if norm_path.lower().startswith(norm_home.lower() + "/"):
        return "~/" + norm_path[len(norm_home) + 1 :]
    return path


def human_count(n: int) -> str:
    return f"{n:,}"
