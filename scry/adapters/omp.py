"""OMP JSONL sessions, named workers, advisors, and adjacent artifacts.

An ``ask`` result is the user's answer, so it is a user turn built from its ``details``
(the text content can be a compaction placeholder); an interrupted ask stays a tool error.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import re

from ..model import Artifact, Call, SessionMeta, ThreadMeta, Turn
from .base import (
    Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource, answer_text,
    compact_json, decode_project_slug, file_fingerprint, first_line,
    iter_jsonl_records, parse_timestamp, plan_jsonl_read, record_guard,
)

_QUESTION_TOOL = "ask"  # its result is the user's answer, so it becomes a user turn


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict)
                     and b.get("type") == "text" and isinstance(b.get("text"), str))


def _answers(details) -> str:
    """The user's answers from an ``ask`` result's details, or "" when they hold none.

    One question keeps its fields at the top level; a multi-question ask lists them in
    ``results``. Selected options are joined, and typed text follows them.
    """
    if not isinstance(details, dict):
        return ""
    entries = details["results"] if isinstance(details.get("results"), list) else [details]
    pairs = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("question"), str):
            continue
        selected = entry.get("selectedOptions")
        answer = ", ".join(str(o) for o in selected) if isinstance(selected, list) else ""
        custom = entry.get("customInput")
        if isinstance(custom, str) and custom.strip():
            answer = f"{answer}; {custom}" if answer else custom
        pairs.append((entry["question"], answer or "(no answer)"))
    return answer_text(pairs)


def _sub_turn(row, ordinal, sub, offset):
    """A ``custom_message`` worth indexing, or None.

    OMP stores two displayable entries outside its message stream: a skill
    invocation (``skill-prompt``, holding the user's own words in
    ``details.args``) and bus mail (``bus:mail``, one line possibly holding
    several mails). Both become sub-turns ``N.k`` so integer ordinals never
    move. Everything else (``bus:card``, ``irc:incoming``, hidden rows) stays
    unindexed, and a skill body is never indexed, only the invocation args.
    """
    if not isinstance(row, dict):
        return None
    if row.get("type") != "custom_message" or not row.get("display"):
        return None
    custom_type = row.get("customType")
    if custom_type not in ("skill-prompt", "bus:mail"):
        return None
    details = row.get("details")
    details = details if isinstance(details, dict) else {}
    ts = parse_timestamp(row.get("timestamp"))
    native_id = row.get("id")
    if custom_type == "skill-prompt":
        attribution = row.get("attribution")
        human = isinstance(attribution, str) and attribution.casefold() in ("user", "human")
        name = details.get("name") if isinstance(details.get("name"), str) else None
        args = details.get("args")
        if not isinstance(args, str):
            content = row.get("content")
            args = content.rsplit("\nUser: ", 1)[-1] if isinstance(content, str) and "\nUser: " in content else ""
        return Turn(ordinal, "user" if human else "system", f"[skill {name or '?'}] {args}",
                    ts=ts, native_id=native_id, offset=offset, sub=sub)
    mails = details.get("mail")
    if isinstance(mails, list) and mails:
        lines = []
        for mail in mails:
            if not isinstance(mail, dict):
                continue
            lines.append(f"from {mail.get('from') or '?'} ({mail.get('relation') or '?'}, {mail.get('mode') or '?'}, "
                         f"omp:{mail.get('session') or '?'}): {mail.get('body') or ''}")
        text = "\n".join(lines)
    else:
        text = ""
    if not text:
        content = row.get("content")
        text = content if isinstance(content, str) else ""
    return Turn(ordinal, "mail", text, ts=ts, native_id=native_id, offset=offset, sub=sub)


def _turn(row, ordinal, offset):
    if not isinstance(row, dict):
        return None
    if row.get("type") == "compaction":
        text = row.get("summary") or row.get("shortSummary")
        if not isinstance(text, str) or not text:
            return None
        return Turn(ordinal, "summary", text, ts=parse_timestamp(row.get("timestamp")),
                    native_id=row.get("id"), offset=offset)
    message = row.get("message")
    if row.get("type") != "message" or not isinstance(message, dict):
        return None
    role = message.get("role")
    speaker = "tool" if role in ("toolResult", "tool") else role
    if speaker not in ("user", "assistant", "tool", "system", "summary"):
        speaker = "system"
    attribution = message.get("attribution", row.get("attribution"))
    human = isinstance(attribution, str) and attribution.casefold() in ("user", "human")
    if message.get("synthetic") or row.get("synthetic") or (role == "user" and attribution is not None and not human):
        speaker = "system"
    content = message.get("content")
    text = _text(content)
    tool = message.get("toolName") if speaker == "tool" else None
    call_id = message.get("toolCallId") if speaker == "tool" else None
    is_error = bool(message.get("isError"))
    if tool == _QUESTION_TOOL and not is_error:
        speaker, text = "user", _answers(message.get("details")) or text
    thinking, calls = [], []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                thinking.append(block["thinking"])
            elif block.get("type") == "toolCall":
                calls.append(Call(str(block.get("name") or ""),
                                  compact_json(block.get("arguments", {})),
                                  str(block.get("intent") or ""), block.get("id")))
    return Turn(ordinal, speaker, text, "\n".join(thinking), tuple(calls),
                parse_timestamp(message.get("timestamp")) or parse_timestamp(row.get("timestamp")),
                tool, call_id, is_error, row.get("id") or message.get("id"), offset)


class OmpAdapter:
    harness = "omp"
    version = "1"  # bump when the same files must map to different turns or threads

    def __init__(self):
        self._facts = {}

    def stores(self, cfg) -> list[StoreSpec]:
        from . import local_stores

        return [s for s in local_stores(cfg) if s.harness == self.harness]

    def discover(self, store, roots=None):
        root = Path(store.location).expanduser()
        if not root.is_dir():
            return
        for path in sorted(root.glob("*/*.jsonl")):
            if roots is not None and str(path) not in roots:
                continue
            session_id = self._scan(str(path))["id"]
            threads = [ThreadSource((), "main", str(path), file_fingerprint(path))]
            self._children(path.with_suffix(""), (), threads)
            yield SessionSource(store.name, session_id, str(path), tuple(threads))

    def _children(self, directory, parent, threads):
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.jsonl")):
            kind = "advisor" if path.stem == "__advisor" or path.stem.startswith("__advisor.") else "subagent"
            agent_path = parent + (path.stem,)
            threads.append(ThreadSource(agent_path, kind, str(path), file_fingerprint(path)))
            self._children(path.with_suffix(""), agent_path, threads)

    def _scan(self, location):
        fingerprint = file_fingerprint(location)
        cached = self._facts.get(location)
        if cached and cached[0] == fingerprint:
            return cached[1]
        memo = getattr(self, "memo", None)
        if memo is not None:
            stored = memo.get(location, fingerprint)
            if stored is not None:
                stored["spawns"] = {k: tuple(v) for k, v in stored["spawns"].items()}
                stored["results"] = {k: tuple(v) if isinstance(v, list) else v for k, v in stored["results"].items()}
                self._facts[location] = (fingerprint, stored)
                return stored
        facts = dict(id=Path(location).stem.rsplit("_", 1)[-1], cwd=None, title="", model="",
                     started=None, ended=None, agent="", task="", extra={}, spawns={}, results={})
        ordinal = 0
        header_title = None
        found_session = False
        for offset, _, row in iter_jsonl_records(location):
            if not isinstance(row, dict):
                continue
            kind = row.get("type")
            ts = parse_timestamp(row.get("timestamp"))
            if ts is not None:
                facts["started"] = ts if facts["started"] is None else min(facts["started"], ts)
                facts["ended"] = ts if facts["ended"] is None else max(facts["ended"], ts)
            if kind == "session" and not found_session:
                found_session = True
                facts["id"] = str(row.get("id") or facts["id"])
                facts["cwd"] = row.get("cwd") or None
                facts["title"] = str(row.get("title") or "")
                if row.get("gitBranch"):
                    facts["extra"]["git_branch"] = str(row["gitBranch"])
            elif kind == "title":
                header_title = str(row.get("title") or "")
            elif kind == "title_change":
                facts["title"] = str(row.get("title") or "")
            elif kind == "model_change":
                facts["model"] = str(row.get("model") or "")
            elif kind == "session_init":
                facts["agent"] = str(row.get("agent") or "")
                facts["task"] = first_line(str(row.get("task") or ""))
                facts["model"] = str(row.get("resolvedModel") or row.get("modelRole") or "")
            turn = _turn(row, ordinal + 1, offset)
            if turn is None:
                continue
            ordinal += 1
            if turn.ts is not None:
                facts["started"] = turn.ts if facts["started"] is None else min(facts["started"], turn.ts)
                facts["ended"] = turn.ts if facts["ended"] is None else max(facts["ended"], turn.ts)
            if turn.speaker == "tool" and turn.call_id:
                facts["results"][turn.call_id] = ordinal
            for call in turn.calls:
                if call.name != "task":
                    continue
                try:
                    args = json.loads(call.args)
                except ValueError:
                    continue
                if not isinstance(args, dict) or not isinstance(args.get("tasks"), list):
                    continue
                for task in args["tasks"]:
                    if isinstance(task, dict) and isinstance(task.get("name"), str):
                        facts["spawns"].setdefault(task["name"], (ordinal, call.call_id))
        if header_title is not None:
            facts["title"] = header_title
        self._facts[location] = (fingerprint, facts)
        if memo is not None:
            memo.put(location, fingerprint, facts)
        return facts

    def session_meta(self, source):
        facts = self._scan(source.root)
        directory = Path(source.root).with_suffix("")
        artifacts = []
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.suffix in (".jsonl", ".tombstone"):
                    continue
                relative = path.relative_to(directory)
                kind = "output" if path.suffix in (".md", ".json") else "log" if re.fullmatch(r"\d+\..+\.log", path.name) else "other"
                owner = relative.parent.parts + ((path.stem,) if kind == "output" else ())
                artifacts.append(Artifact(str(relative), str(path), kind, path.stat().st_size, "/".join(owner)))
        slug = Path(source.root).parent.name if not facts["cwd"] else None
        return SessionMeta(source.store, source.session_id, facts["cwd"] or decode_project_slug(slug), slug,
                           facts["title"], facts["started"], facts["ended"], "",
                           "history://" + source.session_id, source.root, facts["model"], tuple(artifacts), facts["extra"])

    def read_thread(self, source, thread, checkpoint):
        start, ordinal, reset = plan_jsonl_read(thread.location, checkpoint)
        facts = self._scan(thread.location)
        agent_type = facts["agent"]
        if thread.kind == "advisor":
            agent_type = thread.path[-1].partition(".")[2]
        meta = ThreadMeta(thread.path, thread.kind, thread.path[-1] if thread.path else "main",
                          agent_type, facts["task"], facts["model"], facts["started"], facts["ended"],
                          native_id=facts["id"], source=thread.location)
        if thread.kind == "subagent":
            parent = source.thread(thread.path[:-1])
            if parent:
                parent_facts = self._scan(parent.location)
                spawn = parent_facts["spawns"].get(thread.path[-1])
                if spawn:
                    meta = replace(meta, spawn_ordinal=spawn[0], result_ordinal=parent_facts["results"].get(spawn[1]))
        turns = []
        end = start
        native_id = checkpoint.native_id if checkpoint and not reset else None
        sub = 0 if (reset or checkpoint is None) else (checkpoint.sub or 0)
        for offset, end_offset, row in iter_jsonl_records(thread.location, start_offset=start):
            end = end_offset
            turn = _turn(row, ordinal + 1, offset)
            if turn is not None:
                turns.append(turn)
                ordinal += 1
                sub = 0
                native_id = turn.native_id
                continue
            extra = _sub_turn(row, ordinal, sub + 1, offset)
            if extra is not None:
                turns.append(extra)
                sub += 1
                native_id = extra.native_id
        return ThreadRead(meta, turns, Checkpoint(file_fingerprint(thread.location), end, ordinal,
                                                 native_id, guard=record_guard(thread.location, end), sub=sub), reset)
