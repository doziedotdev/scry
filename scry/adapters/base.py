"""The adapter contract. One adapter per harness; the core never reads a transcript itself.

An adapter answers four questions:

1. ``stores(cfg)``            which stores this harness provides on this machine
                              (one per partition, e.g. one per Hermes bank)
2. ``discover(store, roots)`` every session in a store (or only the given roots), as a
                              ``SessionSource``: the root thread plus every child thread,
                              each with a fingerprint that changes when its content changes
3. ``session_meta(source)``   title, project, timestamps, artifacts for the session
4. ``read_thread(...)``       the turns of one thread after a checkpoint, plus the
                              thread's metadata and the new checkpoint

Rules every adapter must keep:

- Never modify a transcript. Open databases read-only.
- Ordinals are 1-based, contiguous, and identical on every read. They only depend
  on the source content, never on index state, so refs are stable everywhere.
- One turn per message as the harness stores it. Thinking, tool calls, and text of
  one assistant message are parts of one turn. A tool result is its own ``tool`` turn.
- A ``user`` turn is a human. Synthetic, injected, or agent-attributed user-role
  messages are ``system``. Harness compaction and summary rows are ``summary``.
- Skip a malformed complete record; stop at an incomplete trailing one (the next
  pass resumes). Truncated or replaced files reset via ``ThreadRead.reset``.
- Redaction is the core's job. Adapters return raw text.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from ..model import SessionMeta, StoreSpec, ThreadMeta, Turn


@dataclass(frozen=True, slots=True)
class ThreadSource:
    path: tuple[str, ...]  # () for the main thread
    kind: str  # main | subagent | advisor | sidechain
    location: str  # file path, or a db uri like sqlite:/path/state.db#session=<id>
    fingerprint: str  # changes whenever the thread's content changes ("size:mtime_ns" or "maxid:N")


@dataclass(frozen=True, slots=True)
class SessionSource:
    store: str
    session_id: str
    root: str  # the session's root location, shown in cards and used for artifact discovery
    threads: tuple[ThreadSource, ...]

    def thread(self, path: tuple[str, ...]) -> ThreadSource | None:
        for thread in self.threads:
            if thread.path == path:
                return thread
        return None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Where the last read of a thread stopped. Stored by the core, interpreted by the adapter."""

    fingerprint: str
    offset: int = 0  # byte offset after the last complete JSONL record; 0 for databases
    ordinal: int = 0  # last integer ordinal emitted
    native_id: str | None = None  # last native message id emitted (databases resume after it)
    guard: str = ""  # record_guard(path, offset) at checkpoint time; a mismatch on resume means the file was replaced
    sub: int = 0  # sub-turns emitted after ``ordinal``; the next one continues the count


@dataclass(slots=True)
class ThreadRead:
    meta: ThreadMeta
    turns: list[Turn]  # only turns after the checkpoint, in (ordinal, sub) order
    checkpoint: Checkpoint  # the checkpoint after these turns are applied
    reset: bool = False  # True: the source was truncated or replaced; purge and start over


class Adapter(Protocol):
    harness: str

    def stores(self, cfg: Any) -> list[StoreSpec]: ...

    def discover(self, store: StoreSpec, roots: set[str] | None = None) -> Iterator[SessionSource]:
        """Every session in the store, or only those whose root is in ``roots`` when given."""
        ...

    def session_meta(self, source: SessionSource) -> SessionMeta: ...

    def read_thread(
        self, source: SessionSource, thread: ThreadSource, checkpoint: Checkpoint | None
    ) -> ThreadRead: ...


# ---------------------------------------------------------------------------
# Shared helpers for JSONL-backed harnesses
# ---------------------------------------------------------------------------


def file_fingerprint(path: str | os.PathLike[str]) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return "missing"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


GUARD_BYTES = 256


def record_guard(path: str | os.PathLike[str], offset: int) -> str:
    """Hash of the bytes just before ``offset``. Cheap proof the file still has the same prefix."""
    if offset <= 0:
        return ""
    import hashlib

    start = max(0, offset - GUARD_BYTES)
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            return hashlib.blake2s(handle.read(offset - start), digest_size=8).hexdigest()
    except OSError:
        return ""


def plan_jsonl_read(path: str, checkpoint: Checkpoint | None) -> tuple[int, int, bool]:
    """Return ``(start_offset, start_ordinal, reset)`` for resuming an append-only file.

    Reset when the file is shorter than the checkpoint offset (truncated) or when the
    bytes before the offset no longer match the checkpoint's guard (replaced in place).
    """
    if checkpoint is None:
        return 0, 0, False
    try:
        size = os.stat(path).st_size
    except OSError:
        return 0, 0, True
    if size < checkpoint.offset:
        return 0, 0, True
    if checkpoint.guard and record_guard(path, checkpoint.offset) != checkpoint.guard:
        return 0, 0, True
    return checkpoint.offset, checkpoint.ordinal, False


def iter_jsonl_records(
    path: str | os.PathLike[str], *, start_offset: int = 0
) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(offset, end_offset, record)`` for complete, newline-terminated JSONL records.

    A malformed complete line is skipped; an incomplete trailing line ends the read
    so the next pass can resume once it is complete.
    """
    with open(path, "rb") as handle:
        handle.seek(max(0, start_offset))
        while True:
            offset = handle.tell()
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                return
            end_offset = handle.tell()
            if not line.strip():
                continue
            try:
                yield offset, end_offset, json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue


def parse_timestamp(value: Any) -> float | None:
    """Unix seconds from a number, an ISO 8601 string (``Z`` allowed), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:  # milliseconds
            number /= 1000.0
        return number if number > 0 else None
    if isinstance(value, str) and value.strip():
        from datetime import datetime, timezone

        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def compact_json(value: Any, limit: int | None = None) -> str:
    """Stable, compact JSON for tool arguments. Lossless unless ``limit`` is given."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return text if limit is None or len(text) <= limit else text[:limit] + "…"


def answer_text(pairs: list[tuple[str, str]]) -> str:
    """A question tool's answers as the user's own turn: one line per answer, then its question.

    The answer leads so a turn's gist shows what was decided; the question follows so one
    search can match both.
    """
    return "\n".join(f"{answer}  ← {question}" if question else answer for question, answer in pairs)


def first_line(text: str, limit: int = 200) -> str:
    """The first substantive line of a task or prompt.

    Harness wrappers put a label line first ("Complete assignment thoroughly:") and task
    templates open with headings ("# Target"); neither says what the task is, so both
    are skipped when a later line exists.
    """
    from ..gist import task_gist

    return task_gist(text, limit)


def decode_project_slug(slug: str, isdir=os.path.isdir, hint: str | None = None) -> str | None:
    """Decode a Claude/Cursor style ``-home-me-my-work`` directory name to a path.

    The encoding is lossy: original hyphens and separators share one representation.
    Resolution order:

    1. ``hint``: a working directory the transcript itself recorded, accepted when its
       own slug equals this slug (so it is the same directory, not a subshell elsewhere).
    2. The local filesystem, greedily, but only when the slug's first two segments name
       a real directory here (``/home/me``): a mirror of another machine must not be
       decoded against this disk, where ``demo-project`` would wrongly split.
    3. The naive split. The caller keeps the slug alongside for these cases.
    """
    text = slug.strip()
    if text.startswith("-"):
        text = text[1:]
    if not text:
        return None
    if hint and hint.startswith("/") and hint.strip("/").replace("/", "-") == text:
        return hint.rstrip("/") or "/"
    parts = text.split("-")
    if not (len(parts) >= 2 and isdir("/" + "/".join(parts[:2]))):
        return "/" + "/".join(parts)
    out: list[str] = []
    i = 0
    while i < len(parts):
        chosen = 1
        for k in range(len(parts) - i, 1, -1):
            candidate = "/" + "/".join(out + ["-".join(parts[i : i + k])])
            if isdir(candidate):
                chosen = k
                break
        out.append("-".join(parts[i : i + chosen]))
        i += chosen
    return "/" + "/".join(out)
