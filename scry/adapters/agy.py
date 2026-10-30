"""Google Antigravity (AGY) JSONL transcripts and subagents, with optional SQLite metadata."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from ..model import Artifact, Call, SessionMeta, ThreadMeta, Turn
from .base import (
    Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource,
    compact_json, file_fingerprint, first_line, iter_jsonl_records,
    parse_timestamp, plan_jsonl_read, record_guard,
)

_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S | re.I)
_MODEL_RE = re.compile(r"setting `Model Selection` from [^\n]+? to ([^\n.]+)\.", re.I)
_SUMMARY_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.S | re.I)
_SYS_MSG_RE = re.compile(
    r"<SYSTEM_MESSAGE>\s*\[Message\]\s*timestamp=(\S+)\s+sender=(\S+)\s+priority=(\S+)\s+content=(.*?)\s*</SYSTEM_MESSAGE>",
    re.S | re.I,
)
_SUBAGENT_CREATED_RE = re.compile(r'"conversationId"\s*:\s*"([A-Za-z0-9_-]+)"')
_TASK_RUNNING_RE = re.compile(r"Tool is running as a background task with task id:\s*(\S+)")
_TASK_DONE_RE = re.compile(r'^Task id "([^"]+)" (?:finished|was canceled) with result:\s*', re.S)
_GENERIC_TAIL_SYS_RE = re.compile(
    r"\n\nThe following is a <SYSTEM_MESSAGE> not actually sent by the user\."
    r"[^\n]*\n\n<SYSTEM_MESSAGE>\s*\[Message\].*</SYSTEM_MESSAGE>\s*\Z",
    re.S,
)


def _transcript_file(brain_dir: Path) -> Path | None:
    logs = brain_dir / ".system_generated" / "logs"
    full = logs / "transcript_full.jsonl"
    if full.is_file():
        return full
    compact = logs / "transcript.jsonl"
    if compact.is_file():
        return compact
    return None


def _uri_to_path(uri: str) -> str | None:
    text = uri.strip()
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urlparse(text)
        path = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        if len(path) >= 2 and path[1] == ":" and path[0].islower():
            path = path[0].upper() + path[1:]
        return path.rstrip("/") or "/"
    return text


def _workspace_path(raw: str) -> str | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
            if isinstance(items, list) and items:
                return _uri_to_path(str(items[0]))
        except ValueError:
            pass
    for part in text.split(","):
        if part.strip():
            return _uri_to_path(part.strip())
    return None


def _clean_user_text(raw: str) -> tuple[str, str]:
    """Extract clean user request text and optional model selection from a USER_INPUT step."""
    model = ""
    m_model = _MODEL_RE.search(raw)
    if m_model:
        model = m_model.group(1).strip()
    m_req = _USER_REQ_RE.search(raw)
    if m_req:
        return m_req.group(1).strip(), model
    return raw.strip(), model


def _clean_checkpoint(raw: str) -> str:
    m = _SUMMARY_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _parse_system_message(raw: str) -> tuple[str | None, str] | None:
    m = _SYS_MSG_RE.search(raw)
    if not m:
        return None
    sender = m.group(2).strip()
    body = m.group(4).strip()
    return sender, body


def _is_generic_error(row: dict, content: str) -> bool:
    if row.get("status") == "ERROR" or row.get("error"):
        return True
    for line in content.splitlines()[:6]:
        stripped = line.strip()
        if stripped.startswith("Command exited with code ") and stripped != "Command exited with code 0.":
            return True
        if stripped.startswith("Encountered error in "):
            return True
    return False


def _sanitize_label(raw: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip()).strip("-")
    if not cleaned or not re.match(r"^[A-Za-z0-9]", cleaned):
        return fallback
    return cleaned[:32]


def _turn_from_row(row: dict, ordinal: int, offset: int, state: dict) -> Turn | None:
    """Convert one Antigravity JSONL step into a normalized ``Turn`` (or ``None`` if duplicate/ignored).

    ``state`` carries ``last_step_index`` and ``pending_calls`` across rows of the thread.
    """
    if not isinstance(row, dict):
        return None
    step_index = row.get("step_index")
    status = str(row.get("status") or "")
    row_type = str(row.get("type") or "")
    source = str(row.get("source") or "")
    # Antigravity occasionally logs duplicate RUNNING rows with the exact same step_index.
    if step_index is not None and step_index == state.get("last_step_index") and status == "RUNNING":
        return None
    if step_index is not None:
        state["last_step_index"] = step_index

    ts = parse_timestamp(row.get("created_at"))
    native_id = str(step_index) if step_index is not None else None
    content = str(row.get("content") or "")

    if row_type == "USER_INPUT" or source == "USER_EXPLICIT":
        text, model = _clean_user_text(content)
        if model:
            state["model"] = model
        return Turn(ordinal, "user", text, ts=ts, native_id=native_id, offset=offset)

    if row_type == "PLANNER_RESPONSE":
        thinking = str(row.get("thinking") or "")
        raw_calls = row.get("tool_calls")
        calls: list[Call] = []
        pending: list[tuple[str, str]] = []
        if isinstance(raw_calls, list):
            for idx, tc in enumerate(raw_calls):
                if not isinstance(tc, dict):
                    continue
                name = str(tc.get("name") or "tool")
                args = tc.get("args", {})
                intent = ""
                if isinstance(args, dict):
                    intent = str(args.get("toolSummary") or args.get("Role") or args.get("Description") or "")
                call_id = f"step-{step_index}-{idx}" if step_index is not None else f"ord-{ordinal}-{idx}"
                calls.append(Call(name, compact_json(args), intent, call_id))
                pending.append((name, call_id))
        state["pending_calls"] = pending
        if not content.strip() and not thinking.strip() and not calls:
            return None
        return Turn(ordinal, "assistant", content, thinking, tuple(calls), ts=ts, native_id=native_id, offset=offset)

    if row_type == "GENERIC":
        pending = state.get("pending_calls") or []
        if pending:
            tool_name, call_id = pending.pop(0)
        else:
            tool_name, call_id = "tool", (f"step-{step_index}-0" if step_index is not None else None)
        content = _GENERIC_TAIL_SYS_RE.sub("", content)
        err = _is_generic_error(row, content)
        if not content and row.get("error"):
            content = str(row["error"])
        return Turn(ordinal, "tool", content, ts=ts, tool=tool_name, call_id=call_id,
                    is_error=err, native_id=native_id, offset=offset)

    if row_type == "CHECKPOINT":
        text = _clean_checkpoint(content)
        if not text:
            return None
        return Turn(ordinal, "summary", text, ts=ts, native_id=native_id, offset=offset)

    if row_type == "ERROR_MESSAGE":
        err_text = str(row.get("error") or content or "API error")
        return Turn(ordinal, "system", f"[error] {err_text}", ts=ts, is_error=True, native_id=native_id, offset=offset)

    if row_type == "SYSTEM_MESSAGE" or source == "SYSTEM":
        parsed = _parse_system_message(content)
        if parsed is not None:
            sender, body = parsed
            # Subagent initial prompt is delivered as a SYSTEM_MESSAGE from the parent session.
            if ordinal == 1 and state.get("is_child") and "/task-" not in sender:
                return Turn(ordinal, "system", body, ts=ts, native_id=native_id, offset=offset)
            m_task = _TASK_DONE_RE.match(body)
            if m_task or "/task-" in sender:
                task_id = m_task.group(1) if m_task else sender
                rest = body[m_task.end():].strip() if m_task else body
                err = (
                    ("Command exited with code " in rest and "Command exited with code 0." not in rest)
                    or "was canceled with result:" in body
                    or "Tool execution was canceled" in rest
                )
                return Turn(ordinal, "tool", rest or body, ts=ts, tool="background_task",
                            call_id=task_id, is_error=err, native_id=native_id, offset=offset)
            return Turn(ordinal, "mail", f"from agy:{sender}: {body}", ts=ts, native_id=native_id, offset=offset)
        if not content.strip():
            return None
        return Turn(ordinal, "system", content.strip(), ts=ts, native_id=native_id, offset=offset)

    if content.strip():
        return Turn(ordinal, "system", content.strip(), ts=ts, native_id=native_id, offset=offset)
    return None


class AgyAdapter:
    harness = "agy"

    def __init__(self, summaries_db: str | Path | None = None) -> None:
        self._explicit_db = Path(summaries_db) if summaries_db is not None else None
        self._facts: dict[str, tuple[str, dict]] = {}

    def stores(self, cfg) -> list[StoreSpec]:
        from . import local_stores

        return [s for s in local_stores(cfg) if s.harness == self.harness]

    def _db_path(self, store: StoreSpec | None, root: str | None = None) -> Path | None:
        if self._explicit_db is not None:
            return self._explicit_db if self._explicit_db.is_file() else None
        if store is not None and store.metadata:
            p = Path(store.metadata).expanduser()
            if p.is_file():
                return p
        if store is not None:
            cand = Path(store.location).expanduser().parent / "conversation_summaries.db"
            if cand.is_file():
                return cand
        if root is not None:
            # <agy>/brain/<id>/.system_generated/logs/transcript_full.jsonl -> <agy>/conversation_summaries.db
            try:
                cand = Path(root).parents[4] / "conversation_summaries.db"
                if cand.is_file():
                    return cand
            except IndexError:
                pass
        return None

    def _db_summaries(self, db_path: Path | None) -> dict[str, dict[str, Any]]:
        if db_path is None or not db_path.is_file():
            return {}
        out: dict[str, dict[str, Any]] = {}
        try:
            conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only = ON")
                for row in conn.execute(
                    "SELECT conversation_id, title, workspace_uris, status, agent_name, parent_conversation_id "
                    "FROM conversation_summaries"
                ):
                    cid = str(row["conversation_id"] or "")
                    if cid:
                        out[cid] = dict(row)
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        return out

    def _scan(self, location: str, is_child: bool = False) -> dict[str, Any]:
        """Scan one transcript file once per fingerprint and cache its structural facts."""
        fingerprint = file_fingerprint(location)
        cached = self._facts.get(location)
        if cached and cached[0] == fingerprint:
            return cached[1]
        memo = getattr(self, "memo", None)
        memo_key = f"{location}#{'child' if is_child else 'main'}#v2"
        if memo is not None:
            stored = memo.get(memo_key, fingerprint)
            if stored is not None:
                stored["spawns"] = {k: dict(v) for k, v in stored.get("spawns", {}).items()}
                self._facts[location] = (fingerprint, stored)
                return stored

        session_id = Path(location).parents[2].name
        facts: dict[str, Any] = {
            "id": session_id,
            "model": "",
            "started": None,
            "ended": None,
            "first_sender": "",
            "first_task": "",
            "spawns": {},  # child_id -> {spawn_ordinal, result_ordinal, role, type_name, prompt, model}
        }
        state: dict[str, Any] = {"last_step_index": None, "pending_calls": [], "is_child": is_child, "model": ""}
        ordinal = 0
        pending_invoke: list[dict[str, Any]] = []

        for offset, _, row in iter_jsonl_records(location):
            if not isinstance(row, dict):
                continue
            if ordinal == 0 and row.get("type") == "SYSTEM_MESSAGE":
                parsed = _parse_system_message(str(row.get("content") or ""))
                if parsed is not None:
                    sender, body = parsed
                    if "/task-" not in sender:
                        facts["first_sender"] = sender
                        facts["first_task"] = first_line(body)

            turn = _turn_from_row(row, ordinal + 1, offset, state)
            if turn is None:
                continue
            ordinal += 1
            if state.get("model"):
                facts["model"] = state["model"]
            if turn.ts is not None:
                facts["started"] = turn.ts if facts["started"] is None else min(facts["started"], turn.ts)
                facts["ended"] = turn.ts if facts["ended"] is None else max(facts["ended"], turn.ts)

            if row.get("type") == "PLANNER_RESPONSE":
                for tc in row.get("tool_calls") or ():
                    if isinstance(tc, dict) and tc.get("name") == "invoke_subagent":
                        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                        pending_invoke.append({
                            "spawn_ordinal": ordinal,
                            "role": str(args.get("Role") or ""),
                            "type_name": str(args.get("TypeName") or ""),
                            "prompt": first_line(str(args.get("Prompt") or "")),
                            "model": str(args.get("Model") or ""),
                        })
            elif row.get("type") == "GENERIC" and pending_invoke:
                content = str(row.get("content") or "")
                m_sub = _SUBAGENT_CREATED_RE.search(content)
                if m_sub:
                    inv = pending_invoke.pop(0)
                    child_id = m_sub.group(1)
                    facts["spawns"][child_id] = {**inv, "result_ordinal": None}
            elif row.get("type") == "SYSTEM_MESSAGE":
                parsed = _parse_system_message(str(row.get("content") or ""))
                if parsed is not None:
                    sender, _ = parsed
                    if sender in facts["spawns"]:
                        facts["spawns"][sender]["result_ordinal"] = ordinal

        self._facts[location] = (fingerprint, facts)
        if memo is not None:
            memo.put(memo_key, fingerprint, facts)
        return facts

    def discover(self, store: StoreSpec, roots: set[str] | None = None) -> Iterator[SessionSource]:
        root = Path(store.location).expanduser()
        if not root.is_dir():
            return
        summaries = self._db_summaries(self._db_path(store))
        files: dict[str, Path] = {}
        for brain_dir in sorted(root.iterdir()):
            if not brain_dir.is_dir():
                continue
            tf = _transcript_file(brain_dir)
            if tf is not None:
                files[brain_dir.name] = tf
        if not files:
            return

        parent_of: dict[str, str] = {}
        children_of: dict[str, list[str]] = {cid: [] for cid in files}
        spawn_meta: dict[tuple[str, str], dict[str, Any]] = {}

        # 1. Link parent -> child from parent transcript `invoke_subagent` calls (works even without DB).
        for cid, tf in files.items():
            facts = self._scan(str(tf), is_child=False)
            for child_id, info in facts["spawns"].items():
                if child_id in files and child_id != cid:
                    parent_of[child_id] = cid
                    if child_id not in children_of[cid]:
                        children_of[cid].append(child_id)
                    spawn_meta[(cid, child_id)] = info

        # 2. Link parent -> child from child's initial SYSTEM_MESSAGE sender or conversation_summaries.db.
        for cid, tf in files.items():
            if cid in parent_of:
                continue
            db_parent = str((summaries.get(cid) or {}).get("parent_conversation_id") or "")
            if db_parent and db_parent in files and db_parent != cid:
                parent_of[cid] = db_parent
                if cid not in children_of[db_parent]:
                    children_of[db_parent].append(cid)
                continue
            facts = self._scan(str(tf), is_child=True)
            sender = facts.get("first_sender") or ""
            if sender in files and sender != cid:
                parent_of[cid] = sender
                if cid not in children_of[sender]:
                    children_of[sender].append(cid)

        for root_id, root_tf in sorted(files.items()):
            if root_id in parent_of:
                continue
            threads = [ThreadSource((), "main", str(root_tf), file_fingerprint(root_tf))]
            used_segments: set[tuple[str, ...]] = set()

            def _add_children(parent_id: str, parent_path: tuple[str, ...], seen: set[str]) -> None:
                for child_id in children_of.get(parent_id, ()):
                    if child_id in seen or child_id not in files:
                        continue
                    info = spawn_meta.get((parent_id, child_id), {})
                    db_info = summaries.get(child_id, {})
                    raw_name = info.get("role") or info.get("type_name") or db_info.get("agent_name") or f"sub-{child_id[:8]}"
                    seg = _sanitize_label(str(raw_name), f"sub-{child_id[:8]}")
                    candidate = (*parent_path, seg)
                    if candidate in used_segments:
                        seg = f"{seg}-{child_id[:8]}"
                        candidate = (*parent_path, seg)
                    used_segments.add(candidate)
                    child_tf = files[child_id]
                    threads.append(ThreadSource(candidate, "subagent", str(child_tf), file_fingerprint(child_tf)))
                    _add_children(child_id, candidate, seen | {child_id})

            _add_children(root_id, (), {root_id})
            if roots is not None and not any(t.location in roots for t in threads):
                continue
            yield SessionSource(store.name, root_id, str(root_tf), tuple(threads))

    def session_meta(self, source: SessionSource) -> SessionMeta:
        facts = self._scan(source.root, is_child=False)
        summaries = self._db_summaries(self._db_path(None, source.root))
        row = summaries.get(source.session_id, {})
        title = str(row.get("title") or "")
        project = _workspace_path(str(row.get("workspace_uris") or ""))
        raw_status = str(row.get("status") or "")
        status = "live" if "RUNNING" in raw_status else ("ended" if raw_status and "IDLE" not in raw_status else "")

        brain_dir = Path(source.root).parents[2]
        artifacts: list[Artifact] = []
        if brain_dir.is_dir():
            for path in sorted(brain_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(brain_dir)
                if not rel.parts or rel.parts[0].startswith("."):
                    continue
                if path.name.endswith(".metadata.json") or path.name.startswith(".resolved"):
                    continue
                kind = "output" if path.suffix in (".md", ".json", ".html") and rel.parts[0] != "scratch" else "other"
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                artifacts.append(Artifact(str(rel).replace("\\", "/"), str(path), kind, size))

        return SessionMeta(
            store=source.store,
            session_id=source.session_id,
            project=project,
            title=title,
            started=facts["started"],
            ended=facts["ended"],
            status=status,
            native_handle=f"conversation://{source.session_id}",
            source=source.root,
            model=facts["model"],
            artifacts=tuple(artifacts),
        )

    def read_thread(self, source: SessionSource, thread: ThreadSource, checkpoint: Checkpoint | None) -> ThreadRead:
        is_child = bool(thread.path)
        start, ordinal, reset = plan_jsonl_read(thread.location, checkpoint)
        facts = self._scan(thread.location, is_child=is_child)
        child_id = facts["id"]

        agent_type = ""
        task = facts.get("first_task") or ""
        model = facts.get("model") or ""
        label = thread.path[-1] if thread.path else "main"
        spawn_ordinal = None
        result_ordinal = None

        if is_child:
            summaries = self._db_summaries(self._db_path(None, source.root))
            db_row = summaries.get(child_id, {})
            if db_row.get("agent_name"):
                agent_type = str(db_row["agent_name"])
            parent = source.thread(thread.path[:-1])
            if parent is not None:
                parent_facts = self._scan(parent.location, is_child=bool(parent.path))
                spawn_info = parent_facts["spawns"].get(child_id)
                if spawn_info:
                    spawn_ordinal = spawn_info.get("spawn_ordinal")
                    result_ordinal = spawn_info.get("result_ordinal")
                    agent_type = str(spawn_info.get("type_name") or agent_type)
                    task = str(spawn_info.get("prompt") or task)
                    if spawn_info.get("model") and spawn_info["model"] != "inherit":
                        model = str(spawn_info["model"])
                    if spawn_info.get("role"):
                        label = str(spawn_info["role"])

        meta = ThreadMeta(
            path=thread.path,
            kind=thread.kind,
            label=label,
            agent_type=agent_type,
            task=task if is_child else "",
            model=model,
            started=facts["started"],
            ended=facts["ended"],
            native_id=child_id,
            source=thread.location,
            spawn_ordinal=spawn_ordinal,
            result_ordinal=result_ordinal,
        )

        # Replay state up to `start` so `pending_calls` and `last_step_index` are accurate on incremental resume.
        state: dict[str, Any] = {"last_step_index": None, "pending_calls": [], "is_child": is_child, "model": ""}
        if start > 0 and not reset:
            replay_ord = 0
            for offset, stop, row in iter_jsonl_records(thread.location):
                if offset >= start:
                    break
                if _turn_from_row(row, replay_ord + 1, offset, state) is not None:
                    replay_ord += 1

        turns: list[Turn] = []
        end = start
        native_id = checkpoint.native_id if checkpoint and not reset else None
        for offset, stop, row in iter_jsonl_records(thread.location, start_offset=start):
            end = stop
            turn = _turn_from_row(row, ordinal + 1, offset, state)
            if turn is not None:
                turns.append(turn)
                ordinal += 1
                native_id = turn.native_id

        return ThreadRead(
            meta=meta,
            turns=turns,
            checkpoint=Checkpoint(
                fingerprint=file_fingerprint(thread.location),
                offset=end,
                ordinal=ordinal,
                native_id=native_id,
                guard=record_guard(thread.location, end),
            ),
            reset=reset,
        )
