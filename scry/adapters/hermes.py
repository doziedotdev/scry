"""Read-only Hermes profile banks, with one conversation per stored session."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator

from .base import Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource, compact_json, first_line, parse_timestamp
from ..model import Call, SessionMeta, ThreadMeta, Turn

_SUMMARY = re.compile(r"[\"']?_compressed_summary[\"']?\s*[:=]\s*(?:1|true)\b", re.I)


def _connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _location(path: str, session: str) -> str:
    return f"sqlite:{path}#session={session}"


def _split(location: str) -> tuple[str, str]:
    path, session = location.removeprefix("sqlite:").rsplit("#session=", 1)
    return path, session


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _flag(value: Any) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no"} if isinstance(value, str) else bool(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else (str(value) if value is not None else "")


def _maxid(db: sqlite3.Connection, session: str) -> int:
    return db.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?", (session,)).fetchone()[0]


def _calls(raw: Any) -> tuple[Call, ...]:
    items = _json(raw)
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return ()
    calls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or item
        if not isinstance(function, dict):
            continue
        name = function.get("name") or item.get("tool_name")
        if name:
            args = _json(function.get("arguments", function.get("input", "")))
            calls.append(Call(str(name), compact_json(args), call_id=item.get("id")))
    return tuple(calls)


def _turn(row: dict, ordinal: int, child: bool) -> Turn:
    text = _text(row.get("content"))
    role = row["role"]
    # ``compacted=1`` marks a message that a later compaction folded away, not a summary;
    # the summary itself carries the ``_compressed_summary`` marker.
    if _flag(row.get("_compressed_summary")) or _SUMMARY.search(text):
        role = "summary"
    elif role in {"tool", "toolResult"}:
        role = "tool"
    elif role not in {"user", "assistant", "system", "summary"}:
        role = "system"
    elif role == "user":
        metadata = _json(row.get("display_metadata"))
        metadata = metadata if isinstance(metadata, dict) else {}
        if child or _flag(metadata.get("synthetic")) or metadata.get("attribution") not in (None, "user", "human") or row.get("display_kind") in {"system", "injected", "async_delegation"}:
            role = "system"
    payload = _json(text) if role == "tool" else None
    error = isinstance(payload, dict) and (_flag(payload.get("is_error")) or bool(payload.get("error")) or payload.get("success") is False)
    return Turn(ordinal, role, text, _text(row.get("reasoning") or row.get("reasoning_content")),
                _calls(row.get("tool_calls")), parse_timestamp(row.get("timestamp")),
                row.get("tool_name") if role == "tool" else None,
                row.get("tool_call_id") if role == "tool" else None,
                bool(error), str(row["id"]))


def _delegation(db: sqlite3.Connection, session: dict) -> dict:
    parent = session.get("parent_session_id")
    if not parent or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'").fetchone():
        return {}
    first = db.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1", (session["id"],)).fetchone()
    goal = first[0] if first else None
    matches = []
    for row in db.execute("SELECT * FROM async_delegations WHERE parent_session_id=?", (parent,)):
        item = dict(row)
        event = _json(item.get("event_json"))
        event = event if isinstance(event, dict) else {}
        if event.get("child_session_id") == session["id"] or (goal and goal in (event.get("goal"), event.get("task"))):
            matches.append({**item, **event})
    return matches[0] if len(matches) == 1 else {}


def _thread_meta(db: sqlite3.Connection, thread: ThreadSource, session: dict) -> ThreadMeta:
    delegation = _delegation(db, session)
    task = delegation.get("task") or delegation.get("goal") or ""
    spawn = result = None
    parent = session.get("parent_session_id")
    delegation_id = delegation.get("delegation_id")
    if parent and delegation_id:
        pending = {}
        for ordinal, row in enumerate(db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (parent,)), 1):
            row = dict(row)
            for call in _calls(row.get("tool_calls")):
                if call.call_id:
                    pending[call.call_id] = ordinal
            payload = _json(row.get("content"))
            if isinstance(payload, dict) and payload.get("delegation_id") == delegation_id:
                if row["role"] == "tool":
                    spawn = pending.get(row.get("tool_call_id"), spawn)
                if payload.get("status") in {"completed", "failed", "error", "cancelled"}:
                    result = ordinal
    return ThreadMeta(thread.path, thread.kind,
                      label=_text(delegation.get("name") or first_line(_text(task)) or session.get("source") or session["id"][:12]) if thread.path else "",
                      task=first_line(_text(task)), agent_type=_text(delegation.get("role")),
                      model=_text(session.get("model")), started=parse_timestamp(session.get("started_at")),
                      ended=parse_timestamp(session.get("ended_at")), status=_text(delegation.get("state") or ("ended" if session.get("ended_at") else "")),
                      native_id=session["id"], source=thread.location, spawn_ordinal=spawn, result_ordinal=result)


class HermesAdapter:
    harness = "hermes"

    def stores(self, cfg) -> list[StoreSpec]:
        from . import local_stores

        return [s for s in local_stores(cfg) if s.harness == self.harness]

    def discover(self, store: StoreSpec, roots: set[str] | None = None) -> Iterator[SessionSource]:
        # A bank is one file; ``roots`` cannot narrow it, and one indexed query lists every session.
        path = str(Path(store.location).expanduser())
        if not Path(path).is_file():
            return
        with closing(_connect(path)) as db:
            for root in db.execute("SELECT id FROM sessions WHERE parent_session_id IS NULL ORDER BY id"):
                root_id = root[0]
                threads = []
                pending = [(root_id, ())]
                seen = set()
                while pending:
                    session, agent_path = pending.pop()
                    if session in seen:
                        continue
                    seen.add(session)
                    threads.append(ThreadSource(agent_path, "subagent" if agent_path else "main", _location(path, session), f"maxid:{_maxid(db, session)}"))
                    children = db.execute("SELECT id FROM sessions WHERE parent_session_id=? ORDER BY id", (session,)).fetchall()
                    pending.extend((r[0], (*agent_path, r[0])) for r in reversed(children))
                yield SessionSource(store.name, root_id, _location(path, root_id), tuple(threads))

    def session_meta(self, source: SessionSource) -> SessionMeta:
        path, session = _split(source.root)
        with closing(_connect(path)) as db:
            row = dict(db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone())
        return SessionMeta(source.store, source.session_id, project=row.get("cwd") or None,
                           title=_text(row.get("title")), started=parse_timestamp(row.get("started_at")),
                           ended=parse_timestamp(row.get("ended_at")), status="ended" if row.get("ended_at") else "",
                           native_handle=session, source=source.root, model=_text(row.get("model")),
                           extra={key: str(row[key]) for key in ("end_reason", "git_branch") if row.get(key)})

    def read_thread(self, source: SessionSource, thread: ThreadSource, checkpoint: Checkpoint | None) -> ThreadRead:
        path, session = _split(thread.location)
        with closing(_connect(path)) as db:
            db.execute("BEGIN")
            row = dict(db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone())
            maximum = _maxid(db, session)
            native = int(checkpoint.native_id or 0) if checkpoint else 0
            prefix = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND id<=?", (session, native)).fetchone()[0]
            reset = checkpoint is not None and (maximum < native or prefix != checkpoint.ordinal)
            if reset:
                native = prefix = 0
            turns = [_turn(dict(message), ordinal, bool(thread.path)) for ordinal, message in enumerate(
                db.execute("SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id", (session, native)), prefix + 1)]
            meta = _thread_meta(db, thread, row)
        last = turns[-1].native_id if turns else (str(native) if native else None)
        return ThreadRead(meta, turns, Checkpoint(f"maxid:{maximum}", 0, prefix + len(turns), last), reset)
