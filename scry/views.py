"""Every view renders a small text block whose lines carry refs, plus a ``next:`` line.

Zoom levels, cheapest first:

    overview   what is here                     find     pointers into sessions
    card       one session or agent, one screen  walk     one gist per turn, paged
    read       full turns, capped per turn       tree     the agent tree
    links      everything a session points at    export   whole thread to a file

A view never exceeds its budget; whatever it leaves out, it names the ref that
recovers it.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from .budget import Out, ago, cut, cut_block, duration, fmt_clock, fmt_span, fmt_ts, human_count, short_path
from .config import Config
from .gist import call_gist, human_size
from .index import LIVE_WINDOW, Index, Resolved, like_prefix
from .model import GRAMMAR, Call, Ref
from .query import FindResult

TOOLISH = ("tool",)  # speakers folded in a walk
FOLD_MIN = 3  # consecutive tool-activity turns before a walk folds them


def tlabel(row) -> str:
    """The turn number as refs print it: ``N`` or ``N.k`` for a sub-turn."""
    sub = row["sub"] if "sub" in row.keys() else 0
    return f"{row['ordinal']}.{sub}" if sub else str(row["ordinal"])


def olabel(ordinal: int, sub: int | None) -> str:
    """The same label from a stored ordinal plus its sub-turn index."""
    return f"{ordinal}.{sub}" if sub else str(ordinal)


@dataclass(slots=True)
class View:
    kind: str
    text: str
    refs: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class Renderer:
    def __init__(self, index: Index, cfg: Config, *, now: float | None = None) -> None:
        self.index = index
        self.cfg = cfg
        self.now = now or time.time()
        self.home = str(cfg.home)
        self._short_cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------- helpers

    def short(self, store: str, session_id: str) -> str:
        """Shortest unique prefix of a session id inside its store, at least 8 characters."""
        key = (store, session_id)
        if key in self._short_cache:
            return self._short_cache[key]
        result = session_id
        for length in (8, 11, 14, 18, 24):
            if length >= len(session_id):
                break
            prefix = session_id[:length]
            n = self.index.db.execute("SELECT COUNT(*) FROM session WHERE store=? AND session_id LIKE ? ESCAPE '\\'", (store, like_prefix(prefix))).fetchone()[0]
            if n == 1:
                result = prefix
                break
        self._short_cache[key] = result
        return result

    def sref(self, row, path: str = "") -> str:
        base = f"{row['store']}:{self.short(row['store'], row['session_id'])}"
        return f"{base}/{path}" if path else base

    def session_line(self, row, *, width: int = 0) -> str:
        span = fmt_span(row["started"], row["activity"] or row["ended"] or row["last_ts"], now=self.now)
        title = row["title"] or row["first_user"]
        head = f"{self.sref(row)}  {span}  {short_path(row['project'], self.home)}  {row['turn_count']} turns"
        if row["thread_count"] and row["thread_count"] > 1:
            head += f"  {row['thread_count'] - 1} agents"
        if self.is_live(row):
            head += "  LIVE"
        if title:
            room = max(24, (width or 160) - len(head) - 4)
            head += f'  "{cut(title, room)}"'
        return head

    def is_live(self, row) -> bool:
        activity = row["activity"] or 0
        return row["status"] != "ended" and activity and (self.now - activity) < LIVE_WINDOW

    # ------------------------------------------------------------ overview

    def overview(self, budget: int, *, searched: tuple[str, ...] | None = None) -> View:
        out = Out(budget)
        stores = self.index.stores()
        total_sessions = sum(int(s["sessions"]) for s in stores)
        total_turns = sum(int(s["turns"]) for s in stores)
        first = min((s["first_ts"] for s in stores if s["first_ts"]), default=None)
        last = max((s["last_ts"] for s in stores if s["last_ts"]), default=None)
        out.add(f"transcripts: {len(stores)} stores · {human_count(total_sessions)} sessions · {human_count(total_turns)} turns · {fmt_ts(first, now=self.now)} → {fmt_ts(last, now=self.now)} (times UTC)")
        for s in stores:
            flag = "" if searched is None or s["name"] in searched else "  (excluded by default; add store=" + s["name"] + ")"
            out.add(f"  {s['name']:<16} {int(s['sessions']):>5} sessions {int(s['turns']):>8} turns  last {ago(s['last_ts'], now=self.now):<8} {short_path(s['location'], self.home)}{flag}")
        projects = self.index.projects()
        if projects:
            out.add("projects (most recent first):")
            for p in projects[:10]:
                out.add(f"  {short_path(p['project'], self.home):<40} {int(p['sessions']):>4} sessions  {int(p['turns'] or 0):>7} turns  last {ago(p['last_ts'], now=self.now)}  [{p['stores']}]")
            if len(projects) > 10:
                out.add(f"  … {len(projects) - 10} more: view=projects")
        recent = self.index.sessions(stores=None, project=None, since=None, until=None, limit=5)
        refs: list[str] = []
        if recent:
            out.add("latest sessions:")
            for row in recent:
                out.add("  " + self.session_line(row, width=150))
                refs.append(self.sref(row))
        out.add("refs: " + GRAMMAR)
        out.add("next: q=<words> to find · ref=<session> for its card · view=projects · view=live")
        return View("overview", out.text(), refs)

    # ---------------------------------------------------------------- find

    def find(self, result: FindResult, budget: int, *, q: str, scope_note: str, speakers: tuple[str, ...]) -> View:
        out = Out(budget)
        refs: list[str] = []
        terms = " ".join(("-" if t.negate else "") + (f'"{t.text}"' if t.phrase else t.text + ("*" if t.prefix else "")) for t in result.terms)
        n_sessions = len(result.groups)
        tier = {"turn": "all terms in one turn", "session": "all terms within one session", "any": "any term", "none": "no searchable terms"}[result.tier]
        out.add(f"find {terms}  ·  {result.total_hits} hits in {n_sessions} sessions  ·  match: {tier}  ·  speakers: {','.join(speakers)}{scope_note}")
        if result.per_term:
            counts = " · ".join(f"{k} {v}" for k, v in result.per_term.items())
            out.add(f"per term (all stores, these speakers): {counts}")
        if not result.groups:
            out.add("nothing matched. Try fewer or different words, speaker=any to include tool output and reasoning, or drop since/store/project.")
            return View("find", out.text(), refs, {"tier": result.tier, "hits": 0})
        # Fill greedily: each session gets as many of its hits as fit, and the first
        # session is guaranteed at least its header line and best hit at any budget.
        refs = []
        shown = 0
        for group in result.groups:
            row = self.index.session_row(group.session_pk)
            if row is None:
                continue
            header = self.session_line(row, width=max(60, min(150, out.room - 20))) + f"  [{group.total} hits]"
            if not out.add(header if shown else cut(header, max(60, out.room - 60)), force=shown == 0):
                break
            shown += 1
            refs.append(self.sref(row))
            for i, hit in enumerate(group.hits):
                when = fmt_clock(hit.ts) if hit.ts else "--:--"
                ref = str(hit.ref.with_store(hit.store, self.short(hit.store, hit.session_id)))
                at = ref[ref.index("@") :] if not hit.thread_path else f"/{hit.thread_path}{ref[ref.index('@'):]}"
                room = out.room if (shown > 1 or i > 0) else out.budget - out.used  # first hit of first session may use the trailer room
                prefix = f"  {at:<10} {when} {hit.speaker:<9} «"
                snippet = cut(hit.snippet, max(30, min(150, room - len(prefix) - 4)))
                if not out.add(f"{prefix}{snippet}»", force=(shown == 1 and i == 0)):
                    break
                refs.append(ref)
        if shown < n_sessions:
            out.add(f"… {n_sessions - shown} more sessions: raise budget, or narrow with since/project/store", force=True)
        first = result.groups[0]
        row = self.index.session_row(first.session_pk)
        head = self.sref(row) if row else ""
        out.add(f"next: ref={head} (card) · ref={head} view=walk speaker=user · ref={first.best.ref.with_store(first.best.store, self.short(first.best.store, first.best.session_id))} (read)", force=True)
        return View("find", out.text(), refs, {"tier": result.tier, "hits": result.total_hits, "sessions": n_sessions, "shown": shown})

    # ---------------------------------------------------------------- card

    def card(self, resolved: Resolved, budget: int) -> View:
        out = Out(budget)
        row = self.index.session_row(resolved.session_pk)
        threads = self.index.thread_rows(resolved.session_pk)
        thread = self.index.thread_row(resolved.thread_pk)
        base = self.sref(row)
        this_ref = f"{base}/{thread['path']}" if thread["path"] else base
        refs = [this_ref]
        if thread["path"]:
            return self._agent_card(out, row, thread, threads, base, this_ref, budget)
        speakers = json.loads(row["speakers"] or "{}")
        tools = json.loads(row["tools"] or "{}")
        files = json.loads(row["files"] or "{}")
        artifacts = json.loads(row["artifacts"] or "[]")
        extra = json.loads(row["extra"] or "{}")
        state = "LIVE" if self.is_live(row) else (row["status"] or "ended")
        out.add(f"{base}  {short_path(row['project'], self.home)}  {fmt_span(row['started'], row['activity'] or row['ended'], now=self.now)} ({duration(row['started'], row['activity'] or row['ended']) or '?'})  {state}, last activity {ago(row['activity'], now=self.now)}  {row['model']}".rstrip())
        facts = [f"native {row['native_handle']}" if row["native_handle"] else "", *(f"{k} {v}" for k, v in extra.items() if v)]
        facts = [f for f in facts if f]
        if facts:
            out.add("  " + " · ".join(facts))
        if row["title"]:
            out.add(f"title: {cut(row['title'], 160)}")
        if row["summary"]:
            out.add(f"summary @{row['summary_ordinal']}: {cut(row['summary'], 400)}")
        sp = " · ".join(f"{k} {v}" for k, v in sorted(speakers.items(), key=lambda kv: -kv[1]))
        main_count = sum(speakers.values())
        main_int = thread["turn_count"]
        sub_extra = main_count - main_int
        with_agents = f" ({row['turn_count']} with agents)" if row["turn_count"] > main_int else ""
        out.add(f"turns {main_int}" + (f" +{sub_extra} sub-turns" if sub_extra else "") + f"{with_agents}: {sp}")
        if tools:
            out.add("tools: " + " ".join(f"{k}×{v}" for k, v in list(tools.items())[:12]))
        runs = [t for t in threads if t["kind"] == "workflow"]
        children = [t for t in threads if t["path"] and t["kind"] != "workflow"]
        if runs:
            out.add(f"workflows ({len(runs)}):")
            landed = 0
            for t in sorted(runs, key=lambda t: t["spawn_ordinal"] or 10**9)[:12]:
                if out.add(self._agent_line(t, base)):
                    refs.append(f"{base}/{t['path']}")
                    landed += 1
            if landed < len(runs):
                out.add(f"  … {len(runs) - landed} more: view=tree", force=True)
        if children:
            names = {t["path"] for t in runs}
            direct = [t for t in children if t["path"].split("/")[0] not in names]
            out.add(f"agents ({len(children)}" + (f", {len(children) - len(direct)} in workflows" if runs else "") + "):")
            landed = 0
            for t in direct[:12]:
                if out.add(self._agent_line(t, base)):
                    refs.append(f"{base}/{t['path']}")
                    landed += 1
            if landed < len(children):
                out.add(f"  … {len(children) - landed} more: view=tree", force=True)
        if files.get("written"):
            out.add("files written: " + cut(" ".join(files["written"]), 300))
        if artifacts:
            names = [a["name"] for a in artifacts if a.get("kind") == "output"] or [a["name"] for a in artifacts]
            logs = sum(1 for a in artifacts if a.get("kind") == "log")
            out.add(f"artifacts: {cut(' '.join(names[:8]), 200)}" + (f" · {logs} tool logs" if logs else ""))
        if row["first_user"]:
            out.add(f"first user @{olabel(row['first_user_ordinal'], row['first_user_sub'])}: {cut(row['first_user'], 220)}")
        if row["last_user"] and (row["last_user_ordinal"], row["last_user_sub"]) != (row["first_user_ordinal"], row["first_user_sub"]):
            out.add(f"last user @{olabel(row['last_user_ordinal'], row['last_user_sub'])}: {cut(row['last_user'], 220)}")
        if row["last_assistant"]:
            out.add(f"last assistant @{row['last_assistant_ordinal']}: {cut(row['last_assistant'], max(200, out.room - 320))}")
        intents = self._recent_intents(resolved.thread_pk, 6)
        if intents:
            out.add("recent activity: " + cut(" · ".join(intents), max(120, out.room - 200)))
        tail = f"@{max(1, resolved.turn_count - 2)}-{resolved.turn_count}" if resolved.turn_count else ""
        out.add(f"next: ref={base}{tail} (read the end) · ref={base} view=walk speaker=user · ref={base} view=links", force=True)
        return View("card", out.text(), refs, {"session": base})

    def _agent_card(self, out: Out, row, thread, threads, base: str, this_ref: str, budget: int) -> View:
        kind = thread["kind"]
        out.add(f"{this_ref}  {kind}{(' ' + thread['agent_type']) if thread['agent_type'] else ''}  {thread['turn_count']} turns  {fmt_span(thread['started'], thread['last_ts'] or thread['ended'], now=self.now)}  {thread['status'] or ''}  {thread['model']}".rstrip())
        out.add(f"  in session {base}  {short_path(row['project'], self.home)}")
        if thread["task"]:
            out.add(f"task: {cut(thread['task'], 300)}")
        if thread["spawn_ordinal"]:
            out.add(f"spawned at {base}@{thread['spawn_ordinal']}" + (f" · result delivered at {base}@{thread['result_ordinal']}" if thread["result_ordinal"] else " · no result turn found"))
        nested = [t for t in threads if t["path"].startswith(thread["path"] + "/")]
        refs = [this_ref]
        if nested:
            out.add(f"agents ({len(nested)}):")
            landed = 0
            for t in nested[:10]:
                if out.add(self._agent_line(t, base)):
                    refs.append(f"{base}/{t['path']}")
                    landed += 1
            if landed < len(nested):
                out.add(f"  … {len(nested) - landed} more: view=tree", force=True)
        turns = self.index.turns(resolved_thread := thread["id"], 1, thread["turn_count"], speakers=("user", "assistant", "summary"))
        first = next((t for t in turns if t["speaker"] in ("user", "system")), None) or (turns[0] if turns else None)
        last = next((t for t in reversed(turns) if t["speaker"] == "assistant"), None)
        if first is not None:
            out.add(f"first {first['speaker']} @{tlabel(first)}: {cut(first['gist'], 220)}")
        if last is not None:
            out.add(f"last assistant @{tlabel(last)}: {cut(last['text'], max(200, out.room - 260)) or last['gist']}")
        intents = self._recent_intents(resolved_thread, 6)
        if intents:
            out.add("recent activity: " + cut(" · ".join(intents), max(120, out.room - 120)))
        tail = f"@{max(1, thread['turn_count'] - 2)}-{thread['turn_count']}" if thread["turn_count"] else ""
        out.add(f"next: ref={this_ref}{tail} (read the end) · ref={this_ref} view=walk · ref={base} (parent card)", force=True)
        return View("card", out.text(), refs, {"session": base, "agent": thread["path"]})

    def _agent_line(self, t, base: str) -> str:
        depth = t["path"].count("/")
        label = t["label"] or t["path"].rsplit("/", 1)[-1]
        bits = [f"{'  ' * depth}  /{t['path']}", t["kind"] + (f" {t['agent_type']}" if t["agent_type"] else ""), f"{t['turn_count']} turns"]
        if t["status"]:
            bits.append(t["status"])
        if t["spawn_ordinal"]:
            bits.append(f"@{t['spawn_ordinal']}→" + (f"@{t['result_ordinal']}" if t["result_ordinal"] else "?"))
        line = "  ".join(bits)
        if t["task"]:
            line += f'  "{cut(t["task"], 90)}"'
        elif label and label != t["path"].rsplit("/", 1)[-1]:
            line += f"  {label}"
        return line

    def _recent_intents(self, thread_pk: int, n: int) -> list[str]:
        rows = self.index.db.execute(
            "SELECT calls_json FROM turn WHERE thread_pk=? AND calls_json<>'[]' ORDER BY ordinal DESC, sub DESC LIMIT ?", (thread_pk, n)
        ).fetchall()
        out: list[str] = []
        for row in reversed(rows):
            for call in json.loads(row["calls_json"])[:1]:
                out.append(call_gist(Call(call["name"], call.get("args", ""), call.get("intent", ""), call.get("id")), 60))
        return out

    # ---------------------------------------------------------------- tree

    def tree(self, resolved: Resolved, budget: int) -> View:
        out = Out(budget)
        row = self.index.session_row(resolved.session_pk)
        threads = self.index.thread_rows(resolved.session_pk)
        base = self.sref(row)
        runs = {t["path"]: t for t in threads if t["kind"] == "workflow"}
        count = f"{len(threads) - 1 - len(runs)} agents" + (f" · {len(runs)} workflows" if runs else "")
        out.add(f"tree {base}  {short_path(row['project'], self.home)}  {count}")
        refs = [base]
        main = next((t for t in threads if not t["path"]), None)
        if main is not None:
            out.add(f"  main  {main['turn_count']} turns  {fmt_span(main['started'], main['last_ts'], now=self.now)}  {main['model']}".rstrip())
        shown = 0
        def place(t):
            """Spawn order; a run's agents directly below it, in the order they started."""
            top = t["path"].split("/")[0]
            run = runs.get(top)
            return ((run or t)["spawn_ordinal"] or 10**9, top if run else t["path"], t["path"] != top, t["started"] or 0, t["path"])

        for t in sorted((t for t in threads if t["path"]), key=place):
            if not out.add(self._agent_line(t, base)):
                break
            refs.append(f"{base}/{t['path']}")
            shown += 1
        rest = len(threads) - 1 - shown
        if rest > 0:
            out.add(f"  … {rest} more threads: raise budget", force=True)
        out.add(f"next: ref={base}/<agent> (card) · ref={base}/<agent> view=walk", force=True)
        return View("tree", out.text(), refs)

    # ---------------------------------------------------------------- walk

    def walk(self, resolved: Resolved, budget: int, *, speakers: tuple[str, ...] | None, start: int | None, limit: int, fold: bool = True) -> View:
        out = Out(budget)
        row = self.index.session_row(resolved.session_pk)
        thread = self.index.thread_row(resolved.thread_pk)
        base = self.sref(row)
        this_ref = f"{base}/{thread['path']}" if thread["path"] else base
        count = resolved.turn_count
        first = start or (resolved.turns[0] if resolved.turns else 1)
        last = resolved.turns[1] if resolved.turns and not start else count
        single = start is None and resolved.turns is not None and resolved.ref.turns is not None and resolved.ref.turns.single
        speaker_set = None if not speakers or "any" in speakers else tuple(s for s in speakers if s != "thinking")
        rows = self.index.turns(resolved.thread_pk, first, last, speakers=speaker_set, subs=not single)
        label = "user+assistant+tool" if speaker_set is None else "+".join(speaker_set)
        out.add(f"walk {this_ref}  {count} turns  showing {label} from @{first}" + ("  (tool runs folded)" if fold and speaker_set is None else ""))
        refs = [this_ref]
        lines: list[tuple[int, int, int, str]] = []  # (first ordinal, last ordinal, last sub, line)
        run: list = []

        def flush_run() -> None:
            if not run:
                return
            if len(run) < FOLD_MIN or not fold:
                for r in run:
                    lines.append((r["ordinal"], r["ordinal"], r["sub"] or 0, self._walk_line(r)))
            else:
                names = Counter()
                gists = []
                for r in run:
                    if r["speaker"] == "tool":
                        names[r["tool"] or "tool"] += 1
                    for call in json.loads(r["calls_json"])[:1]:
                        gists.append(call.get("intent") or call_gist(Call(call["name"], call.get("args", ""), "", None), 40))
                errors = sum(1 for r in run if r["is_error"])
                head = f"@{run[0]['ordinal']}-{run[-1]['ordinal']}".ljust(10)
                summary = " ".join(f"{k}×{v}" for k, v in names.most_common(5))
                line = f"{head} [{len(run)} tool turns: {summary}{f' · {errors} errors' if errors else ''}]"
                if gists:
                    line += "  " + cut(" · ".join(gists), 140)
                lines.append((run[0]["ordinal"], run[-1]["ordinal"], 0, line))
            run.clear()

        for r in rows:
            toolish = r["speaker"] == "tool" or (r["speaker"] == "assistant" and r["calls_json"] != "[]" and not r["text"].strip())
            if toolish:
                run.append(r)
                continue
            flush_run()
            lines.append((r["ordinal"], r["ordinal"], r["sub"] or 0, self._walk_line(r)))
        flush_run()
        shown = 0
        last_shown = first - 1
        last_sub = 0
        for lo, hi, hi_sub, line in lines:
            if shown >= limit or not out.add(line):
                break
            shown += 1
            last_shown = hi
            last_sub = hi_sub
        if shown == len(lines):
            last_shown = last
            last_sub = 0
        if last_shown < last:
            resume = f"{last_shown}" if last_sub else f"{last_shown + 1}"
            out.add(f"more: ref={this_ref}@{resume}- view=walk" + (f" speaker={','.join(speaker_set)}" if speaker_set else ""), force=True)
        out.add(f"next: ref={this_ref}@<N> (read a turn) · ref={this_ref}@<N>-<M> (read a range) · ref={this_ref} (card)", force=True)
        return View("walk", out.text(), refs, {"from": first, "to": last_shown, "count": count})

    def _walk_line(self, r) -> str:
        head = f"@{tlabel(r)}".ljust(8) + f"{fmt_clock(r['ts'])} {r['speaker']:<9} "
        size = f" [{human_size(r['chars'])}]" if r["chars"] > 400 else ""
        return head + cut(r["gist"], 140) + size

    # ---------------------------------------------------------------- read

    def read(self, resolved: Resolved, budget: int, *, full: bool, speakers: tuple[str, ...] | None = None) -> View:
        out = Out(budget)
        row = self.index.session_row(resolved.session_pk)
        thread = self.index.thread_row(resolved.thread_pk)
        base = self.sref(row)
        this_ref = f"{base}/{thread['path']}" if thread["path"] else base
        first, last = resolved.turns or (1, resolved.turn_count)
        if resolved.sub is not None:
            picked = self.index.turn(resolved.thread_pk, first, resolved.sub)
            rows = [picked] if picked is not None else []
            span = f"@{first}.{resolved.sub}"
        else:
            single = resolved.ref.turns is not None and resolved.ref.turns.single
            rows = self.index.turns(resolved.thread_pk, first, last, subs=not single)
            span = f"@{first}" if first == last else f"@{first}-{last}"
        out.add(f"read {this_ref}{span}  of {resolved.turn_count} turns  {short_path(row['project'], self.home)}" + ("  (full)" if full else ""))
        refs = [f"{this_ref}{span}"]
        shown: tuple[int, int] = (first - 1, 0)
        want_thinking = bool(speakers and ("thinking" in speakers or "any" in speakers))
        reserve = 0  # the forced trailer lines draw on TRAILER_RESERVE
        for i, r in enumerate(rows):
            ref = f"{this_ref}@{tlabel(r)}"
            head = f"── {ref}  {r['speaker']}  {fmt_ts(r['ts'], now=self.now)}"
            if r["speaker"] == "tool":
                head += f"  {r['tool'] or ''}{'  ERROR' if r['is_error'] else ''}"
            # Each remaining turn gets an equal share of what is left. Call previews and
            # the text are cut to fit inside that share after the fixed lines, so a long
            # turn shows its head instead of vanishing behind a "raise budget" line.
            # Every line costs its length plus a newline; the block itself costs one more.
            share = (out.room - reserve) // (len(rows) - i) - 1
            calls = json.loads(r["calls_json"])
            text = r["text"].rstrip()
            thinking = r["thinking"] or ""
            thinking_line = None
            if thinking and not (want_thinking or full):
                thinking_line = f"[thinking {human_size(len(thinking))}: ref={ref} speaker=thinking]"
            left = share - len(head) - 1 - (len(thinking_line) + 1 if thinking_line else 0)
            call_lines = []
            if calls:
                text_keep = min(len(text) + 1, max(100, left // 2)) if text else 0
                thinking_keep = min(len(thinking) + 12, 100) if thinking and not thinking_line else 0
                line_limit = None if full else max(24, (left - text_keep - thinking_keep) // len(calls) - 1)
                for call in calls:
                    args = call.get("args", "")
                    line = f"→ {call['name']}" + (f" ({call['intent']})" if call.get("intent") else "") + (f": {args}" if args else "")
                    call_lines.append(line if line_limit is None else cut(line, min(line_limit, 320)))
                left -= sum(len(c) + 1 for c in call_lines)
            parts = [head]
            if text:
                if full or len(text) + 1 <= left:
                    parts.append(text)
                else:
                    marker = len(f"\n… [+{len(text):,} chars: ref={ref} full=true]")
                    parts.append(cut_block(text, max(24, left - 1 - marker), f"ref={ref} full=true"))
                left -= len(parts[-1]) + 1
            if thinking:
                if thinking_line:
                    parts.append(thinking_line)
                elif full or len(thinking) + 12 <= left:
                    parts.append("[thinking]\n" + thinking)
                else:
                    marker = len(f"\n… [+{len(thinking):,} chars: ref={ref} full=true speaker=thinking]")
                    parts.append("[thinking]\n" + cut_block(thinking, max(24, left - 12 - marker), f"ref={ref} full=true speaker=thinking"))
            parts.extend(call_lines)
            block = "\n".join(parts)
            if not out.add_block(block, reserve=reserve):
                # The share was too small even for the floors: name the turn and its size.
                what = " ".join(w for w in (f"{human_size(len(text))} text" if text else "", f"{len(calls)} calls" if calls else "", f"{human_size(len(thinking))} thinking" if thinking else "") if w)
                if not out.add_block(f"{head}\n[{what}: ref={ref} full=true]", reserve=reserve):
                    break
            shown = (r["ordinal"], r["sub"] or 0)
            refs.append(ref)
        tail_row: tuple[int, int] = (rows[-1]["ordinal"], rows[-1]["sub"] or 0) if rows else (first - 1, 0)
        if rows and shown < tail_row:
            if resolved.sub is not None:
                out.add(f"more: ref={this_ref}@{first}.{resolved.sub}" + (" full=true" if full else " (or raise budget)"), force=True)
            elif last >= first:
                resume = f"{shown[0]}" if shown[1] else f"{shown[0] + 1}"
                out.add(f"more: ref={this_ref}@{resume}-{last}" + (" full=true" if full else " (or raise budget)"), force=True)
        if resolved.sub is not None:
            prev = f"ref={this_ref}@{max(1, first - 2)}-{first}" if first >= 1 else ""
            after = max(1, first)
            nxt = [prev, f"ref={this_ref}@{after}-{min(resolved.turn_count, after + 2)}" if after <= resolved.turn_count else "", f"ref={this_ref} view=walk"]
        else:
            nxt = [f"ref={this_ref}@{max(1, first - 3)}-{first - 1}" if first > 1 else "", f"ref={this_ref}@{last + 1}-{min(resolved.turn_count, last + 3)}" if last < resolved.turn_count else "", f"ref={this_ref} view=walk"]
        out.add("next: " + " · ".join(n for n in nxt if n), force=True)
        return View("read", out.text(), refs, {"from": first, "to": shown[0]})

    # --------------------------------------------------------------- links

    def links(self, resolved: Resolved, budget: int) -> View:
        out = Out(budget)
        row = self.index.session_row(resolved.session_pk)
        threads = self.index.thread_rows(resolved.session_pk)
        thread = self.index.thread_row(resolved.thread_pk)
        base = self.sref(row)
        this_ref = f"{base}/{thread['path']}" if thread["path"] else base
        refs = [this_ref]
        out.add(f"links {this_ref}")
        if thread["path"]:
            parent_path = thread["path"].rsplit("/", 1)[0] if "/" in thread["path"] else ""
            parent_ref = f"{base}/{parent_path}" if parent_path else base
            out.add(f"parent: {parent_ref}" + (f"  spawned @{thread['spawn_ordinal']}" if thread["spawn_ordinal"] else "") + (f"  result @{thread['result_ordinal']}" if thread["result_ordinal"] else ""))
            refs.append(parent_ref)
        children = [t for t in threads if t["path"].startswith((thread["path"] + "/") if thread["path"] else "") and t["path"] and t["path"] != thread["path"]]
        if children:
            depth = thread["path"].count("/") + 1 if thread["path"] else 0
            shown = [t for t in children if t["path"].count("/") == depth][:12]
            more = f"  … {len(children) - len(shown)} more: view=tree" if len(children) > len(shown) else ""
            if out.add(f"agents: " + " ".join(f"{base}/{t['path']}" for t in shown) + more):
                refs.extend(f"{base}/{t['path']}" for t in shown)
            else:
                out.add(f"  … {len(children)} agents: view=tree", force=True)
        if row["native_handle"]:
            out.add(f"native: {row['native_handle']}  source: {short_path(row['source'], self.home)}")
        artifacts = json.loads(row["artifacts"] or "[]")
        if artifacts:
            outs = [a for a in artifacts if a.get("kind") == "output"]
            logs = [a for a in artifacts if a.get("kind") == "log"]
            if outs:
                out.add("artifacts: " + cut(" ".join(short_path(a["path"], self.home) for a in outs[:6]), 400))
            if logs:
                out.add(f"tool logs: {len(logs)} files in {short_path(os.path.dirname(logs[0]['path']), self.home)}")
        near = self.index.neighbours(resolved.session_pk)
        if near:
            out.add("same project, overlapping time:")
            for n in near:
                out.add("  " + self.session_line(n, width=150))
                refs.append(self.sref(n))
        mentions = self.index.mentions(row["session_id"], exclude_pk=resolved.session_pk)
        if mentions:
            out.add("mentioned by:")
            seen = set()
            for m in mentions:
                r = f"{m['store']}:{self.short(m['store'], m['session_id'])}" + (f"/{m['path']}" if m["path"] else "") + f"@{olabel(m['ordinal'], m['sub'])}"
                if r in seen:
                    continue
                seen.add(r)
                out.add(f"  {r}  {ago(m['activity'], now=self.now)}")
                refs.append(r)
        out.add(f"next: ref=<any ref above> · ref={base} view=tree", force=True)
        return View("links", out.text(), refs)

    # ------------------------------------------------------------ listings

    def sessions(self, rows, budget: int, *, title: str, live: bool = False) -> View:
        out = Out(budget)
        out.add(title)
        refs: list[str] = []
        shown = 0
        for row in rows:
            line = self.session_line(row, width=150)
            if live:
                last = self.index.db.execute(
                    "SELECT t.gist, t.speaker FROM turn t JOIN thread th ON th.id=t.thread_pk WHERE th.session_pk=? AND th.path='' ORDER BY t.ordinal DESC, t.sub DESC LIMIT 1", (row["id"],)
                ).fetchone()
                if last:
                    line += f"\n    last: {last['speaker']} {cut(last['gist'], 120)}"
            if not out.add_block(line, reserve=60):
                break
            refs.append(self.sref(row))
            shown += 1
        if shown < len(rows):
            out.add(f"… {len(rows) - shown} more: raise budget or narrow with project/since", force=True)
        if not rows:
            out.add("none." if not live else "no session active in the last 15 minutes.")
        out.add("next: ref=<session> (card) · ref=<session> view=walk speaker=user", force=True)
        return View("live" if live else "sessions", out.text(), refs)

    def projects(self, rows, budget: int) -> View:
        out = Out(budget)
        out.add(f"projects: {len(rows)}  (sessions · turns · last activity · stores)")
        shown = 0
        for p in rows:
            if not out.add(f"  {short_path(p['project'], self.home):<44} {int(p['sessions']):>4} · {int(p['turns'] or 0):>7} · {ago(p['last_ts'], now=self.now):<7} · {p['stores']}"):
                break
            shown += 1
        if shown < len(rows):
            out.add(f"… {len(rows) - shown} more: raise budget", force=True)
        out.add("next: project=<name> view=sessions · q=<words> project=<name>", force=True)
        return View("projects", out.text(), [])

    def skills(self, entries: list[dict], budget: int, *, total: int, total_uses: int, since_ts: float | None, until_ts: float | None,
               stores: tuple[str, ...], scope_note: str = "") -> View:
        """Skill usage, most used first. ``entries`` come from ``Index.skills``; each gains a ``last_ref``."""
        out = Out(budget)
        refs: list[str] = []
        skills: list[dict] = []
        for e in entries:
            store, session_id, path, ordinal, sub = e["last"]
            last_ref = f"{self.sref({'store': store, 'session_id': session_id}, path)}@{olabel(ordinal, sub)}"
            skills.append({"name": e["name"], "uses": e["uses"], "invoked": e["invoked"], "read": e["read"], "sessions": e["sessions"],
                           "threads": e["threads"], "first_ts": e["first_ts"], "last_ts": e["last_ts"], "last_ref": last_ref})
        more = f" (top {len(skills)} of {total})" if total > len(skills) else ""
        out.add(f"skills: {total} used · {total_uses} uses{more}{scope_note}")
        if skills:
            out.add(f"  {'name':<28} {'uses':>5} {'inv':>4} {'read':>5} {'sess':>5}  main/sub/adv/side  last")
        shown = 0
        for s in skills:
            t = s["threads"]
            split = f"{t['main']}/{t['subagent']}/{t['advisor']}/{t['sidechain']}"
            line = (f"  {cut(s['name'], 28):<28} {s['uses']:>5} {s['invoked']:>4} {s['read']:>5} {s['sessions']:>5}  {split:<17}  "
                    f"{ago(s['last_ts'], now=self.now):<8} ref={s['last_ref']}")
            if not out.add(line):
                break
            refs.append(s["last_ref"])
            shown += 1
        if shown < len(skills):
            out.add(f"… {len(skills) - shown} more: raise budget or use --json", force=True)
        if not skills:
            out.add("none: no skill invocations or skill reads in scope.")
        first = f"ref={skills[0]['last_ref']} (read the latest use) · " if skills else ""
        out.add(f"next: {first}q=<skill name> (find mentions) · view=skills since=7d", force=True)
        meta = {"skills": skills, "total": total, "since_ts": since_ts, "until_ts": until_ts, "stores": list(stores)}
        return View("skills", out.text(), refs, meta)

    # -------------------------------------------------------------- export

    def export(self, resolved: Resolved, path: str | None) -> View:
        row = self.index.session_row(resolved.session_pk)
        thread = self.index.thread_row(resolved.thread_pk)
        base = self.sref(row)
        this_ref = f"{base}/{thread['path']}" if thread["path"] else base
        first, last = resolved.turns or (1, resolved.turn_count)
        if resolved.sub is not None:
            picked = self.index.turn(resolved.thread_pk, first, resolved.sub)
            rows = [picked] if picked is not None else []
            span = f"@{first}.{resolved.sub}"
        else:
            single = resolved.ref.turns is not None and resolved.ref.turns.single
            rows = self.index.turns(resolved.thread_pk, first, last, subs=not single)
            span = f"@{first}-{last}" if resolved.turns else ""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", this_ref + span)
        target = path or os.path.join(self.cfg.export_dir, f"{safe}.md")
        os.makedirs(os.path.dirname(target) or ".", mode=0o700, exist_ok=True)
        chunks = [f"# {this_ref}  {short_path(row['project'], self.home)}  {fmt_span(row['started'], row['activity'], now=self.now)}\n"]
        for r in rows:
            chunks.append(f"\n## @{tlabel(r)} {r['speaker']}{('  ' + (r['tool'] or '')) if r['speaker'] == 'tool' else ''}{'  ERROR' if r['is_error'] else ''}  {fmt_ts(r['ts'], now=self.now)}\n")
            if r["thinking"]:
                chunks.append(f"<thinking>\n{r['thinking']}\n</thinking>\n")
            if r["text"]:
                chunks.append(r["text"] + "\n")
            for call in json.loads(r["calls_json"]):
                chunks.append(f"\n→ {call['name']}" + (f" ({call['intent']})" if call.get("intent") else "") + f"\n{call.get('args', '')}\n")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("".join(chunks))
        os.chmod(target, 0o600)
        size = os.path.getsize(target)
        text = f"exported {this_ref}{span or f'@{first}-{last}'}  {len(rows)} turns  {size:,} bytes → {target}\nnext: read it with your file reader in ranges; it is redacted like the index."
        return View("export", text, [this_ref], {"path": target, "bytes": size, "turns": len(rows)})

