"""Claude Code JSONL adapter.

Observed layout: <project>/<session>.jsonl and
<project>/<session>/subagents/workflows/<run>/agent-*.jsonl.
Discovery also accepts subagents/**/agent-*.jsonl. In-file sidechains use a
#sidechain=<root uuid> logical location. Local-command text is kept as system.
An AskUserQuestion result is the user's answer, so it is a user turn built from
``toolUseResult.answers``; a declined question stays a tool error.

A session file also holds transport rows. ``api-request-blob`` copies a request's
messages with no timestamp or uuid; they are not turns, so ``_records`` drops their
payload. A message typed mid-turn has no ``user`` row: it is a ``queued_command``
attachment. Thinking text is often absent (only a signature), leaving rows with
nothing to show; those are not turns either.

A workflow run is a thread of kind ``workflow`` at path ``(<run>,)``, its agents below
it at ``(<run>, agent-*)``. The run keeps its script in workflows/scripts/, each
agent's result in subagents/workflows/<run>/journal.jsonl, and its final result in
workflows/<run>.json once it ends. The agents' own transcripts end in a tool call,
so without this thread their results would only be reachable with speaker=any.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator

from ..model import Artifact, Call, SessionMeta, ThreadMeta, Turn
from .base import (
    Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource, answer_text,
    compact_json, decode_project_slug, file_fingerprint, first_line,
    iter_jsonl_records, parse_timestamp, plan_jsonl_read, record_guard,
)

_MARKER = "#sidechain="
_INJECTED = ("<system-reminder>", "<system-instruction>", "<system-directive>",
             "<task-notification>", "<teammate-message", "<local-command-",
             "<command-name>", "<user-prompt-submit-hook>", "<agent-message",
             "[Request interrupted", "<bash-stdout>", "<bash-stderr>")
_CONVERSATION = frozenset({None, "user", "assistant", "system", "summary"})
_RUN_RESULT = "workflow-result"  # native id of a run's last turn; a read past it starts over
_RUN_ID = re.compile(r"\bRun ID: (\S+)")
_TASK_ID = re.compile(r"\bTask ID: (\S+)")
_NOTIFIED_TASK = re.compile(r"<task-id>([^<\s]+)</task-id>")
_SCRIPT_FIELD = re.compile(r"""^\s*(name|description):\s*(['"`])(.+?)\2\s*,?\s*(?://.*)?$""", re.M)
_SCRIPT_PHASE = re.compile(r"""\{\s*title:\s*(['"`])(.+?)\1""")
_QUESTION_TOOL = "AskUserQuestion"  # its result is the user's answer, so it becomes a user turn


def _records(path: str) -> list[tuple[int, int, dict]]:
    records = []
    for offset, end, row in iter_jsonl_records(path):
        if isinstance(row, dict):
            if row.get("type") not in _CONVERSATION:
                row.pop("message", None)
            records.append((offset, end, row))
    return records


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _timestamp(value: Any) -> float | None:
    """ISO text, or epoch milliseconds as a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else float(value)
    return parse_timestamp(value)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _last_timestamp(path: Path) -> float | None:
    """The newest row time in a transcript's tail, or its mtime; copies do not keep mtimes."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            window = 65536
            while True:
                handle.seek(max(0, size - window))
                for line in reversed(handle.read().splitlines()):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    stamp = parse_timestamp(row.get("timestamp")) if isinstance(row, dict) else None
                    if stamp is not None:
                        return stamp
                if window >= size:
                    return _mtime(path)
                window *= 8  # a single row can be larger than the tail
    except OSError:
        return None


def _first_timestamp(path: Path) -> float | None:
    """The oldest row time at a transcript's head; rows are written in order."""
    try:
        for _, _, row in iter_jsonl_records(path):
            stamp = parse_timestamp(row.get("timestamp")) if isinstance(row, dict) else None
            if stamp is not None:
                return stamp
    except OSError:
        pass
    return None


def _render(value: Any) -> str:
    """Readable and searchable text for a result: JSON escapes glue ``\n`` onto the next word."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_render(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_render(item) for item in value)
    return json.dumps(value)


def _media(content: Any) -> str:
    """A placeholder, so a message that is only an image still gets its turn."""
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in {"image", "document"} for b in content):
        return "[image]"
    return ""


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and isinstance(block.get("text"), str))
    return ""


def _answers(result: Any) -> str:
    """The user's answers from an AskUserQuestion ``toolUseResult``, or "" when it holds none.

    Notes the user attached to a question are kept verbatim after its answer.
    """
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or not answers:
        return ""
    notes = result.get("annotations")
    notes = notes if isinstance(notes, dict) else {}
    pairs = []
    for question, answer in answers.items():
        answer = answer if isinstance(answer, str) else compact_json(answer)
        if note := notes.get(question):
            answer = f"{answer} (note: {note if isinstance(note, str) else compact_json(note)})"
        pairs.append((str(question), answer))
    return answer_text(pairs)


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
        attachment = row.get("attachment")
        if row.get("type") == "attachment":
            if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
                prompt = attachment.get("prompt")
                text = _text(prompt) or _media(prompt)
                human = attachment.get("humanTurn") or (attachment.get("origin") or {}).get("kind") == "human"
                if text.strip():
                    speaker = "user" if human and not child and not text.lstrip().startswith(_INJECTED) else "system"
                    turns.append(Turn(len(turns) + 1, speaker, text, **common))
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
        text = _text(content) or (_media(content) if role == "user" else "")
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
            tool, is_error = names.get(call_id), bool(block.get("is_error"))
            if tool == _QUESTION_TOOL and not is_error:
                answered = _answers(row.get("toolUseResult")) or _text(block.get("content"))
                turns.append(Turn(len(turns) + 1, "user", answered, tool=tool, call_id=call_id, **common))
                continue
            turns.append(Turn(len(turns) + 1, "tool", _text(block.get("content")),
                              tool=tool, call_id=call_id, is_error=is_error, **common))
        if role == "user" and (row.get("isMeta") or child or row.get("agentId")
                               or row.get("agentName") or row.get("attributionAgent")
                               or text.lstrip().startswith(_INJECTED)):
            role = "system"
        if row.get("isCompactSummary"):
            role = "summary"
        if text or thinking or calls:
            turns.append(Turn(len(turns) + 1, role, text, thinking, tuple(calls), **common))
    return turns


def _artifact_kind(path: Path, session_dir: Path) -> str:
    top = path.relative_to(session_dir).parts[0]
    return {"tool-results": "log", "workflows": "output"}.get(top, "other")


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
    version = "3"  # bump when the same files must map to different turns or threads

    def __init__(self) -> None:
        self._files: dict[str, tuple] = {}
        self._links: dict[str, tuple] = {}

    def stores(self, cfg) -> list[StoreSpec]:
        from . import local_stores

        return [s for s in local_stores(cfg) if s.harness == self.harness]

    def discover(self, store: StoreSpec, roots: set[str] | None = None) -> Iterator[SessionSource]:
        memo = getattr(self, "memo", None)
        # A session can have a file in two projects; its sources are merged, so a scoped pass needs them all.
        names = None if roots is None else {Path(root).name for root in roots}
        paths = sorted(p for p in Path(store.location).expanduser().glob("*/*.jsonl") if _size(p) >= 0)
        primary: dict[str, Path] = {}  # the file whose main thread survives the merge, as in _merge_duplicates
        for path in paths:
            if path.name not in primary or _size(path) > _size(primary[path.name]):
                primary[path.name] = path
        for path in paths:
            if names is not None and path.name not in names:
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
            session_dir = path.with_suffix("")
            runs = {journal.parent: journal for journal in session_dir.glob("subagents/workflows/*/journal.jsonl")}
            children = sorted(session_dir.glob("subagents/**/agent-*.jsonl"))
            tasks = self._main_links(str(primary[path.name]))["tasks"] if children or runs else {}
            for run_dir, journal in sorted(runs.items()):
                threads.append(ThreadSource((run_dir.name,), "workflow", str(journal),
                                            self._run_fingerprint(str(primary[path.name]), session_dir, journal)))
            for child in children:
                if child.parent in runs:
                    threads.append(ThreadSource((child.parent.name, child.stem), "subagent", str(child), file_fingerprint(child)))
                    continue
                label = child.stem
                if label in used:
                    label = "-".join(child.relative_to(session_dir).with_suffix("").parts)
                agent_path = (label,)
                used.add(label)
                # A background agent's result reaches the main thread after its own file stops changing.
                notified = tasks.get(child.stem.removeprefix("agent-"), 0)
                size, _, stamp = file_fingerprint(child).partition(":")
                threads.append(ThreadSource(agent_path, "subagent", str(child),
                                            f"{int(size) + notified}:{stamp}" if stamp else size))
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
            title = next((row["aiTitle"] for row in reversed(rows) if row.get("aiTitle")), "")
        if not title:
            title = next((row["summary"] for row in reversed(rows)
                          if row.get("type") == "summary" and row.get("summary")), "")
        started, ended = _bounds(records)
        session_dir = Path(source.root).with_suffix("")
        artifacts = tuple(Artifact(p.name, str(p), _artifact_kind(p, session_dir), p.stat().st_size)
                          for p in sorted(session_dir.rglob("*"))
                          if p.is_file() and p.suffix != ".jsonl" and not p.name.endswith(".meta.json"))
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

    def _main_links(self, path: str) -> dict:
        """``{"runs": {run: [launch, completion]}, "tasks": {task id: notification}}`` as main-thread turns."""
        fingerprint = file_fingerprint(path)
        cached = self._links.get(path)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        memo = getattr(self, "memo", None)
        key = f"{path}#links{self.version}"
        links = memo.get(key, fingerprint) if memo is not None else None
        if links is None:
            turns = self._main_turns(path)
            tasks: dict[str, int] = {}
            for turn in turns:
                if turn.speaker == "system" and turn.text.lstrip().startswith("<task-notification>"):
                    if task := _NOTIFIED_TASK.search(turn.text):
                        tasks.setdefault(task.group(1), turn.ordinal)
            launches: dict[str, int] = {}
            runs: dict[str, list[int | None]] = {}
            for turn in turns:
                for call in turn.calls:
                    if call.name == "Workflow" and call.call_id:
                        launches[call.call_id] = turn.ordinal
                if turn.speaker == "tool" and turn.call_id in launches and (match := _RUN_ID.search(turn.text)):
                    task = _TASK_ID.search(turn.text)
                    runs.setdefault(match.group(1), [launches[turn.call_id], tasks.get(task.group(1)) if task else None])
            links = {"runs": runs, "tasks": tasks}
            if memo is not None:
                memo.put(key, fingerprint, links)
        self._links[path] = (fingerprint, links)
        return links

    def _run_fingerprint(self, root: str, session_dir: Path, journal: Path) -> str:
        """``size:mtime_ns`` over everything a run's thread is built from.

        The launch and completion turns live in the main thread, which can arrive or move
        after the run's own files stop changing, so their ordinals count towards the size.
        """
        run = journal.parent.name
        spawn, result = self._main_links(root)["runs"].get(run, (None, None))
        size = (spawn or 0) + (result or 0)
        latest = 0
        for path in (journal, session_dir / "workflows" / f"{run}.json",
                     *session_dir.glob(f"workflows/scripts/*{run}.js")):
            try:
                stat = path.stat()
            except OSError:
                continue
            size += stat.st_size
            latest = max(latest, stat.st_mtime_ns)
        return f"{size}:{latest}"

    def _read_run(self, source: SessionSource, thread: ThreadSource, checkpoint: Checkpoint | None) -> ThreadRead:
        """Header, script, one turn per agent result, and the final result once there is one.

        Journal rows carry no time, so a result takes the last time in its agent's transcript.
        A failed or aborted run records ``result: null`` beside ``error``. A run launched from
        an earlier run's ``scriptPath`` has no script file: its script arrives with its record,
        and the guard then restarts the thread, since the header and script sit before turns
        already stored. Paths come from the journal: a merged session's root may be elsewhere.
        """
        journal = Path(thread.location)
        run = thread.path[-1]
        session_dir = journal.parents[3]
        record = _load_json(session_dir / "workflows" / f"{run}.json")
        script_path = next(iter(sorted(session_dir.glob(f"workflows/scripts/*{run}.js"))), None)
        try:
            script = script_path.read_text(encoding="utf-8") if script_path else str(record.get("script") or "")
        except OSError:
            script = str(record.get("script") or "")
        head = script.split("\n}\n", 1)[0]  # the meta block, for a run that has no record yet
        fields: dict[str, str] = {}
        for key, _, value in _SCRIPT_FIELD.findall(head):
            fields.setdefault(key, value.replace("\\'", "'"))
        name = str(record.get("workflowName") or fields.get("name") or run)
        description = str(record.get("summary") or fields.get("description") or "")
        phases = [str(p.get("title")) for p in record.get("phases") or [] if isinstance(p, dict)] \
            or [title for _, title in _SCRIPT_PHASE.findall(head)]
        records = _records(str(journal))
        labels = {row.get("agentId"): row.get("label") or row.get("agentId") for _, _, row in records
                  if row.get("type") == "started"}
        first = next((journal.parent / f"agent-{agent}.jsonl" for agent in labels), None)
        began = _timestamp(record.get("startTime")) or (_first_timestamp(first) if first else None) \
            or (_mtime(script_path) if script_path else None)
        header = "\n".join(line for line in (
            f"workflow {name} ({run})", description, "phases: " + ", ".join(phases) if phases else "") if line)
        turns = [Turn(1, "summary", header, ts=began, native_id=f"{run}-header", offset=0)]
        if script:
            turns.append(Turn(2, "system", script, ts=began, native_id=f"{run}-script", offset=0))
        frame = hashlib.sha256(f"{header}\0{script}".encode("utf-8", "replace")).hexdigest()[:16]
        for offset, _, row in records:
            if row.get("type") not in {"result", "failed"}:
                continue
            agent = str(row.get("agentId") or "")
            body = _render(row.get("result")) if row["type"] == "result" else "failed"
            turns.append(Turn(len(turns) + 1, "assistant", f"{labels.get(agent) or agent}\n{body}",
                              ts=_last_timestamp(journal.parent / f"agent-{agent}.jsonl"),
                              native_id=str(row.get("key") or agent), offset=offset))
        end = records[-1][1] if records else 0
        if record:
            value = record.get("result")
            body = _render(record.get("error") if value is None else value)
            logs = "\n".join(str(line) for line in record.get("logs") or [])
            head = f"{name} {record.get('status') or 'ended'}, {record.get('agentCount', '?')} agents"
            done = _timestamp(record.get("timestamp")) or _mtime(session_dir / "workflows" / f"{run}.json")
            turns.append(Turn(len(turns) + 1, "assistant", "\n".join(filter(None, (head, logs, body))),
                              ts=done, native_id=_RUN_RESULT, offset=end))
            end += 1
        spawn, result = self._main_links(source.root)["runs"].get(run, (None, None))
        stamps = [t.ts for t in turns if t.ts is not None]
        meta = ThreadMeta(thread.path, "workflow", run, agent_type=name, task=description,
                          model=str(record.get("defaultModel") or ""),
                          started=min(stamps, default=None), ended=max(stamps, default=None),
                          status=str(record.get("status") or ""), native_id=run, source=thread.location,
                          spawn_ordinal=spawn, result_ordinal=result)
        reset = False
        start = 0
        if checkpoint is not None:
            if checkpoint.guard and checkpoint.guard != frame:
                reset = True
            elif checkpoint.offset == end:
                start = end + 1
            elif checkpoint.native_id == _RUN_RESULT or checkpoint.offset > end or not checkpoint.offset:
                reset = True  # resumed past its final result, replaced, or first read before any row
            else:
                start = checkpoint.offset
        last = turns[-1]
        new_checkpoint = Checkpoint(thread.fingerprint, end, last.ordinal, last.native_id, frame)
        return ThreadRead(meta, [turn for turn in turns if turn.offset >= start], new_checkpoint, reset)

    def read_thread(self, source: SessionSource, thread: ThreadSource,
                    checkpoint: Checkpoint | None) -> ThreadRead:
        if thread.kind == "workflow":
            return self._read_run(source, thread, checkpoint)
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
        details = _load_json(Path(path).with_suffix(".meta.json")) if thread.kind == "subagent" else {}
        in_run = len(thread.path) > 1 and thread.kind == "subagent"
        if thread.path and prompt and not in_run:
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
                # A background agent's tool result is only a receipt; its notification is the result.
                result = self._main_links(source.root)["tasks"].get(Path(path).stem.removeprefix("agent-"), result)
        task = first_line(prompt) if thread.path else ""
        if in_run:
            agent_type = str(details.get("workflowPhase") or "")
            task = str(details.get("description") or task)
        elif not agent_type:
            agent_type = str(details.get("agentType") or "")
        meta = ThreadMeta(thread.path, thread.kind, thread.path[-1] if thread.path else "",
                          agent_type=agent_type, task=task,
                          model=_model(selected), started=started, ended=ended,
                          native_id=root or Path(path).stem, source=thread.location,
                          spawn_ordinal=spawn, result_ordinal=result)
        last = turns[-1] if turns else None
        end = records[-1][1] if records else 0
        new_checkpoint = Checkpoint(thread.fingerprint if thread.kind == "subagent" else file_fingerprint(path),
                                    end, last.ordinal if last else 0,
                                    last.native_id if last else None, guard=record_guard(path, end))
        return ThreadRead(meta, [turn for turn in turns if turn.offset >= start], new_checkpoint, reset)
