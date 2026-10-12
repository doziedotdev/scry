"""Cursor JSONL messages with optional, keyed SQLite metadata enrichment.

Cursor does not store tool results. turn_ended markers, including errors, are
ignored: they are lifecycle events, not messages. No result turns are invented.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..model import Call, SessionMeta, ThreadMeta, Turn
from .base import (Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource,
                   compact_json, decode_project_slug, file_fingerprint, first_line,
                   iter_jsonl_records, plan_jsonl_read, record_guard)

_TIMESTAMP = re.compile(r"<timestamp\b[^>]*>\s*(.*?)\s*</timestamp\s*>", re.I | re.S)
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_ZONE = re.compile(r"\s*\(UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\)\s*$", re.I)


def parse_cursor_timestamp(text: str) -> float | None:
    match = _TIMESTAMP.search(text)
    if not match:
        return None
    value = html.unescape(match[1]).strip()
    zone = timezone.utc
    offset = _ZONE.search(value)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3] or 0)
        if hours > 23 or minutes > 59:
            return None
        zone = timezone(timedelta(hours=hours, minutes=minutes) * (1 if offset[1] == "+" else -1))
        value = value[:offset.start()].rstrip()
    for weekday in ("%A, ", "%a, ", ""):
        for month in ("%b", "%B"):
            try:
                return datetime.strptime(value, weekday + month + " %d, %Y, %I:%M %p").replace(tzinfo=zone).timestamp()
            except ValueError:
                pass
    return None


def _text(value: str) -> str:
    return html.unescape(_TAG.sub("", _TIMESTAMP.sub("", value))).strip()


def _parts(row: dict) -> tuple[str, str, tuple[Call, ...]]:
    content = row["message"].get("content", [])
    if isinstance(content, str):
        return content, "", ()
    texts, thinking, calls = [], [], []
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "thinking" and isinstance(block.get("thinking"), str):
            thinking.append(block["thinking"])
        elif kind == "tool_use":
            args = block.get("input", {})
            calls.append(Call(str(block.get("name", "")), compact_json(args),
                              str(args.get("i", "")) if isinstance(args, dict) else "",
                              block.get("id")))
    return "\n".join(texts), "\n".join(thinking), tuple(calls)


def _speaker(row: dict, raw: str) -> str:
    message = row["message"]
    role = row.get("role", message.get("role"))
    if role == "summary" or row.get("type") in ("summary", "compaction") or row.get("isSummary"):
        return "summary"
    if role == "user" and (row.get("isSynthetic") or message.get("isSynthetic")
                           or row.get("source") in ("agent", "system")
                           or ("<system-reminder>" in raw and "<user_query>" not in raw)):
        return "system"
    return role if role in ("user", "assistant", "system") else "system"


class CursorAdapter:
    harness = "cursor"

    def __init__(self, state_db: str | Path | None = None):
        self._explicit_db = Path(state_db) if state_db is not None else None
        self._source_dbs: dict[str, Path | None] = {}
        self._slug_hints: dict[str, str] = {}

    def stores(self, cfg) -> list[StoreSpec]:
        from . import local_stores

        return [s for s in local_stores(cfg) if s.harness == self.harness]

    def _metadata(self, source: SessionSource, composer_id: str) -> dict:
        db = self._source_dbs.get(source.root, self._explicit_db)
        if db is None or not db.is_file():
            return {}
        result = {}
        try:
            connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
            try:
                connection.execute("PRAGMA query_only = ON")
                try:
                    row = connection.execute(
                        "SELECT createdAt,lastUpdatedAt,isSubagent,value FROM composerHeaders WHERE composerId = ?",
                        (composer_id,),
                    ).fetchone()
                    if row:
                        result.update(started=row[0] / 1000 if row[0] else None,
                                      ended=row[1] / 1000 if row[1] else None, isSubagent=bool(row[2]))
                        value = json.loads(row[3]) if row[3] else {}
                        if isinstance(value, dict):
                            result.update(value)
                except (sqlite3.Error, ValueError, TypeError):
                    pass
                try:
                    row = connection.execute("SELECT value FROM cursorDiskKV WHERE key = ?",
                                             ("composerData:" + composer_id,)).fetchone()
                    value = json.loads(row[0]) if row else {}
                    if isinstance(value, dict):
                        # Headers are authoritative for names when present.
                        result = {**value, **result}
                except (sqlite3.Error, ValueError, TypeError):
                    pass
            finally:
                connection.close()
        except sqlite3.Error:
            pass
        return result

    def discover(self, store: StoreSpec, roots: set[str] | None = None):
        root = Path(store.location)
        if not root.is_dir():
            return
        db = self._explicit_db or (Path(store.metadata) if store.metadata else None)
        for main in sorted(root.glob("*/agent-transcripts/*/*.jsonl")):
            if main.stem != main.parent.name or main.parents[2].name.isdigit():
                continue
            if roots is not None and str(main) not in roots:
                continue
            threads = [ThreadSource((), "main", str(main), file_fingerprint(main))]
            for child in sorted((main.parent / "subagents").glob("*.jsonl")):
                threads.append(ThreadSource((child.stem,), "subagent", str(child), file_fingerprint(child)))
            source = SessionSource(store.name, main.stem, str(main), tuple(threads))
            self._source_dbs[source.root] = db
            yield source

    def session_meta(self, source: SessionSource) -> SessionMeta:
        info = self._metadata(source, source.session_id)
        slug = Path(source.root).parents[2].name
        turns = self.read_thread(source, source.threads[0], None).turns
        hint = self._slug_hint(Path(source.root).parents[2], turns)
        return SessionMeta(source.store, source.session_id,
                           project=None if slug == "empty-window" else decode_project_slug(slug, hint=hint),
                           project_slug=slug, title=str(info.get("name") or ""),
                           started=info.get("started") or (turns[0].ts if turns else None),
                           ended=info.get("ended") or (turns[-1].ts if turns else None),
                           status=str(info.get("status") or ""), native_handle=source.session_id,
                           source=source.root,
                           extra={"isSubagent": str(info["isSubagent"]).lower()} if "isSubagent" in info else {})

    def _slug_hint(self, project_dir: Path, turns) -> str | None:
        """The real working directory behind a project slug, learned from any session under it.

        Sessions without shell calls carry no cwd of their own; one sibling that has it
        resolves the slug for all of them, so every session of a project shows the same
        path. Memoized per project directory across passes.
        """
        slug = project_dir.name
        memo = getattr(self, "memo", None)
        cached = self._slug_hints.get(slug)
        if cached is None and memo is not None:
            cached = memo.get("slug:" + str(project_dir), slug)
        if cached is not None:
            return cached or None
        hint = next((wd for turn in turns for call in turn.calls if (wd := _working_directory(call)) and _matches(wd, slug)), None)
        if hint is None:
            for main in sorted(project_dir.glob("agent-transcripts/*/*.jsonl")):
                if main.stem != main.parent.name:
                    continue
                for _offset, _end, row in iter_jsonl_records(main):
                    content = (row.get("message") or {}).get("content") if isinstance(row, dict) else None
                    for block in content if isinstance(content, list) else ():
                        if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                            wd = block["input"].get("working_directory") or block["input"].get("cwd")
                            if isinstance(wd, str) and _matches(wd, slug):
                                hint = wd
                                break
                    if hint:
                        break
                if hint:
                    break
        self._slug_hints[slug] = hint or ""
        if memo is not None:
            memo.put("slug:" + str(project_dir), slug, hint or "")
        return hint

    def _thread_meta(self, source: SessionSource, thread: ThreadSource) -> ThreadMeta:
        composer_id = Path(thread.location).stem
        info = self._metadata(source, composer_id)
        label = str(info.get("name") or ("sub-" + composer_id[:8] if thread.path else ""))
        spawn, task, agent_type = None, "", ""
        if thread.path:
            parent = self._metadata(source, source.session_id)
            subinfo = parent.get("subagentInfo", {})
            if isinstance(subinfo, dict):
                child_info = subinfo.get(composer_id, {})
                if isinstance(child_info, dict):
                    label = str(info.get("name") or child_info.get("name") or label)
                    task = first_line(str(child_info.get("prompt") or ""))
                    agent_type = str(child_info.get("subagent_type") or "")
            for turn in self.read_thread(source, source.threads[0], None).turns:
                for call in turn.calls:
                    if call.name != "Task":
                        continue
                    try:
                        args = json.loads(call.args)
                    except ValueError:
                        continue
                    if not isinstance(args, dict):
                        continue
                    if any(args.get(key) in (composer_id, label) for key in
                           ("name", "subagent_name", "subComposerId", "composerId")):
                        spawn = turn.ordinal
                        task = first_line(str(args.get("prompt") or task))
                        agent_type = str(args.get("subagent_type") or agent_type)
                        break
                if spawn is not None:
                    break
        return ThreadMeta(thread.path, thread.kind, label=label, agent_type=agent_type, task=task,
                          started=info.get("started"), ended=info.get("ended"),
                          status=str(info.get("status") or ""), native_id=composer_id,
                          source=thread.location, spawn_ordinal=spawn)

    def read_thread(self, source: SessionSource, thread: ThreadSource,
                    checkpoint: Checkpoint | None) -> ThreadRead:
        path = thread.location
        start, ordinal, reset = plan_jsonl_read(path, checkpoint)
        timestamp = Path(path).stat().st_mtime
        turns, end = [], start
        # Recover the carried timestamp on resumes, without retaining old turns.
        for offset, stop, row in iter_jsonl_records(path):
            if not isinstance(row, dict) or not isinstance(row.get("message"), dict):
                if offset >= start:
                    end = stop
                continue
            raw, thinking, calls = _parts(row)
            timestamp = parse_cursor_timestamp(raw) or timestamp
            if offset < start:
                continue
            end = stop
            ordinal += 1
            speaker = _speaker(row, raw)
            if thread.path and speaker == "user":
                speaker = "system"
            turns.append(Turn(ordinal, speaker, _text(raw), thinking, calls,
                              ts=timestamp, native_id=row.get("id"), offset=offset))
        fingerprint = file_fingerprint(path)
        return ThreadRead(self._thread_meta(source, thread), turns,
                          Checkpoint(fingerprint, end, ordinal, guard=record_guard(path, end)), reset)


def _working_directory(call) -> str | None:
    """The cwd a Cursor shell call recorded, when the call carries one."""
    if not call.args.startswith("{"):
        return None
    try:
        args = json.loads(call.args)
    except ValueError:
        return None
    value = args.get("working_directory") or args.get("cwd") if isinstance(args, dict) else None
    return value if isinstance(value, str) and value.startswith("/") else None


def _matches(cwd: str, slug: str) -> bool:
    """True when ``cwd`` is the directory the slug encodes (its own slug is equal)."""
    return cwd.strip("/").replace("/", "-") == slug.lstrip("-")
