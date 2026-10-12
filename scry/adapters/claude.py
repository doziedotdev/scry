"""Claude Code JSONL adapter.

Observed layout: <project>/<session>.jsonl and
<project>/<session>/subagents/workflows/<workflow>/agent-*.jsonl.
Discovery also accepts subagents/**/agent-*.jsonl. In-file sidechains use a
#sidechain=<root uuid> logical location. Local-command text is kept as system.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ..model import Artifact, Call, SessionMeta, ThreadMeta, Turn
from .base import (
    Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource, compact_json,
    decode_project_slug, file_fingerprint, first_line, iter_jsonl_records,
    parse_timestamp, plan_jsonl_read, record_guard,
)

_MARKER = "#sidechain="
_INJECTED = ("<system-reminder>", "<system-instruction>", "<system-directive>",
             "<task-notification>", "<teammate-message", "<local-command-",
             "<command-name>", "<user-prompt-submit-hook>")


def _records(path: str) -> list[tuple[int, int, dict]]:
    return [(offset, end, row) for offset, end, row in iter_jsonl_records(path)
            if isinstance(row, dict)]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and isinstance(block.get("text"), str))
    return ""


def _groups(records: list[tuple[int, int, dict]]) -> dict[str, str]:
    rows = {row["uuid"]: row for _, _, row in records if row.get("uuid")}
    roots: dict[str, str] = {}
    for uid, row in rows.items():
        if not row.get("isSidechain") or uid in roots:
            continue
        chain: list[str] = []
        current = uid
        while current not in roots and current not in chain:
            chain.append(current)
            parent = rows[current].get("parentUuid")
            if parent not in rows or not rows[parent].get("isSidechain"):
                break
            current = parent
        root = roots.get(current, current)
        for member in chain:
            roots[member] = root
    return roots


def _selected(records: list[tuple[int, int, dict]], kind: str, root: str):
    if kind == "subagent":
        return records
    groups = _groups(records)
    return [(offset, end, row) for offset, end, row in records
            if (groups.get(row.get("uuid")) == root if kind == "sidechain"
                else not row.get("isSidechain"))]


def _turns(records: list[tuple[int, int, dict]], child: bool) -> list[Turn]:
    turns: list[Turn] = []
    names: dict[str, str] = {}
    for offset, _, row in records:
        common = dict(ts=parse_timestamp(row.get("timestamp")),
                      native_id=row.get("uuid"), offset=offset)
        if row.get("type") == "summary" and isinstance(row.get("summary"), str):
            turns.append(Turn(len(turns) + 1, "summary", row["summary"], **common))
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            if row.get("type") == "system" and _text(row.get("content")):
                turns.append(Turn(len(turns) + 1, "system", _text(row["content"]), **common))
            continue
        role = message.get("role", row.get("type"))
        if role not in {"user", "assistant", "tool", "system"}:
            role = "system"
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        text = _text(content)
        thinking = "\n".join(b["thinking"] for b in blocks
                             if b.get("type") == "thinking" and isinstance(b.get("thinking"), str))
        calls = []
        for block in blocks:
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "unknown")
                call_id = block.get("id")
                if call_id:
                    names[call_id] = name
                args = block.get("input", {})
                calls.append(Call(name, compact_json(args),
                                  str(args.get("description") or "") if isinstance(args, dict) else "",
                                  call_id))
        results = [b for b in blocks if b.get("type") == "tool_result"] if role == "user" else []
        for block in results:
            call_id = block.get("tool_use_id")
            turns.append(Turn(len(turns) + 1, "tool", _text(block.get("content")),
                              tool=names.get(call_id), call_id=call_id,
                              is_error=bool(block.get("is_error")), **common))
        if role == "user" and (row.get("isMeta") or child or row.get("agentId")
                               or row.get("agentName") or row.get("attributionAgent")
                               or text.lstrip().startswith(_INJECTED)):
            role = "system"
        if row.get("isCompactSummary"):
            role = "summary"
        if not results or text or thinking or calls:
            turns.append(Turn(len(turns) + 1, role, text, thinking, tuple(calls), **common))
    return turns


def _bounds(records):
    timestamps = [ts for _, _, row in records
                  if (ts := parse_timestamp(row.get("timestamp"))) is not None]
    return (min(timestamps), max(timestamps)) if timestamps else (None, None)


def _model(records):
    return next((row["message"]["model"] for _, _, row in records
                 if isinstance(row.get("message"), dict)
                 and row["message"].get("role") == "assistant" and row["message"].get("model")), "")


class ClaudeAdapter:
    harness = "claude"

    def __init__(self) -> None:
        self._files: dict[str, tuple] = {}

    def stores(self, cfg) -> list[StoreSpec]:
        from . import local_stores

        return [s for s in local_stores(cfg) if s.harness == self.harness]

    def discover(self, store: StoreSpec, roots: set[str] | None = None) -> Iterator[SessionSource]:
        memo = getattr(self, "memo", None)
        for path in sorted(Path(store.location).expanduser().glob("*/*.jsonl")):
            if roots is not None and str(path) not in roots:
                continue
            fingerprint = file_fingerprint(path)
            side = memo.get(str(path), fingerprint) if memo is not None else None
            if side is None:
                side = self._sidechains(str(path))
                if memo is not None:
                    memo.put(str(path), fingerprint, side)
            threads = [ThreadSource((), "main", str(path), fingerprint)]
            used: set[str] = set()
            for label, root in side:
                used.add(label)
                threads.append(ThreadSource((label,), "sidechain", str(path) + _MARKER + root, fingerprint))
            for child in sorted(path.with_suffix("").glob("subagents/**/agent-*.jsonl")):
                label = child.stem
                if label in used:
                    label = "-".join(child.relative_to(path.with_suffix("")).with_suffix("").parts)
                agent_path = (label,)
                used.add(label)
                threads.append(ThreadSource(agent_path, "subagent", str(child), file_fingerprint(child)))
            yield SessionSource(store.name, path.stem, str(path), tuple(threads))

    def _sidechains(self, path: str) -> list[list[str]]:
        """``[label, root uuid]`` per sidechain in a file; parsing the whole file is the cost memoized above."""
        records, groups, _ = self._parsed(path)
        used: set[str] = set()
        out: list[list[str]] = []
        for root in dict.fromkeys(groups.values()):
            row = next(row for _, _, row in records if row.get("uuid") == root)
            label = str(row.get("agentName") or row.get("agentId") or f"sidechain-{root[:8]}").replace("/", "_")
            if label in used:
                label += "-" + root
            used.add(label)
            out.append([label, root])
        return out

    def session_meta(self, source: SessionSource) -> SessionMeta:
        records, _, _ = self._parsed(source.root)
        rows = [row for _, _, row in records]
        project = next((row["cwd"] for row in rows if row.get("cwd")), None)
        slug = Path(source.root).parent.name
        branch = next((row["gitBranch"] for row in rows if row.get("gitBranch")), "")
        title = next((row.get("customTitle") or row.get("title") for row in reversed(rows)
                      if row.get("customTitle") or row.get("title")), "")
        if not title:
            title = next((row["summary"] for row in reversed(rows)
                          if row.get("type") == "summary" and row.get("summary")), "")
        started, ended = _bounds(records)
        artifacts = tuple(Artifact(p.name, str(p), "other", p.stat().st_size)
                          for p in sorted(Path(source.root).with_suffix("").rglob("*"))
                          if p.is_file() and p.suffix != ".jsonl")
        return SessionMeta(source.store, source.session_id, project or decode_project_slug(slug),
                           slug, title, started, ended, native_handle=source.session_id,
                           source=source.root, model=_model(records), artifacts=artifacts,
                           extra={"branch": branch} if branch else {})

    def _parsed(self, path: str) -> tuple[list, dict[str, str], list[Turn] | None]:
        """Records, sidechain groups, and (lazily) main-thread turns of one file, parsed once per fingerprint.

        A session file holds its main thread and every sidechain; reading it once per
        thread made a 200-sidechain session parse its 166 MB file 200 times.
        """
        fingerprint = file_fingerprint(path)
        cached = self._files.get(path)
        if cached is None or cached[0] != fingerprint:
            if len(self._files) >= 4:  # bounded: a pass walks sessions in order, so a few files suffice
                self._files.pop(next(iter(self._files)))
            records = _records(path)
            cached = (fingerprint, records, _groups(records), None)
            self._files[path] = cached
        return cached[1], cached[2], cached[3]

    def _main_turns(self, path: str) -> list[Turn]:
        records, groups, turns = self._parsed(path)
        if turns is None:
            turns = _turns([(o, e, r) for o, e, r in records if not r.get("isSidechain")], False)
            fingerprint, _, _, _ = self._files[path]
            self._files[path] = (fingerprint, records, groups, turns)
        return turns

    def read_thread(self, source: SessionSource, thread: ThreadSource,
                    checkpoint: Checkpoint | None) -> ThreadRead:
        path, _, root = thread.location.partition(_MARKER)
        start, _, reset = plan_jsonl_read(path, checkpoint)
        records, groups, _ = self._parsed(path)
        if thread.kind == "subagent":
            selected = records
        elif thread.kind == "sidechain":
            selected = [(o, e, r) for o, e, r in records if groups.get(r.get("uuid")) == root]
        else:
            selected = [(o, e, r) for o, e, r in records if not r.get("isSidechain")]
        turns = self._main_turns(path) if not thread.path else _turns(selected, True)
        started, ended = _bounds(selected)
        prompt = next((_text(row["message"].get("content")) for _, _, row in selected
                       if isinstance(row.get("message"), dict) and row["message"].get("role") == "user"), "")
        spawn = result = None
        agent_type = ""
        if thread.path and prompt:
            parent_turns = self._main_turns(source.root)
            matches = []
            for turn in parent_turns:
                for call in turn.calls:
                    if call.name not in {"Task", "Agent"}:
                        continue
                    try:
                        args = json.loads(call.args)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(args, dict) and args.get("prompt") == prompt:
                        matches.append((turn, call, args))
            if len(matches) == 1:
                turn, call, args = matches[0]
                spawn = turn.ordinal
                agent_type = str(args.get("subagent_type") or "")
                result = next((t.ordinal for t in parent_turns
                               if t.speaker == "tool" and call.call_id and t.call_id == call.call_id), None)
        meta = ThreadMeta(thread.path, thread.kind, thread.path[-1] if thread.path else "",
                          agent_type=agent_type, task=first_line(prompt) if thread.path else "",
                          model=_model(selected), started=started, ended=ended,
                          native_id=root or Path(path).stem, source=thread.location,
                          spawn_ordinal=spawn, result_ordinal=result)
        last = turns[-1] if turns else None
        end = records[-1][1] if records else 0
        new_checkpoint = Checkpoint(file_fingerprint(path), end, last.ordinal if last else 0,
                                    last.native_id if last else None, guard=record_guard(path, end))
        return ThreadRead(meta, [turn for turn in turns if turn.offset >= start], new_checkpoint, reset)
