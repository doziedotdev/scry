"""scry: one lens over every past and present agent session.

    scry           overview
    scry WORDS...  find
    scry REF       card (session or agent), or read (REF@N, @N-M, @-N)
    scry REF --view walk|tree|links|export
    scry --view sessions|projects|live|skills|index

Every option also works as a leading verb for humans: ``walk REF``, ``read REF``,
``show REF``, ``find WORDS``, ``sessions``, ``projects``, ``live``, ``skills``, ``index``.
Output is plain text with refs; ``--json`` wraps it in ``{schema, view, text, refs, meta}``.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone

from .config import MAX_BUDGET, load_config
from .index import Index
from .model import GRAMMAR, RefError, looks_like_ref, parse_ref
from .query import ALL_SPEAKERS, DEFAULT_SPEAKERS, Scope, find
from .views import Renderer, View

VIEWS = ("auto", "find", "card", "walk", "read", "tree", "links", "sessions", "projects", "live", "skills", "overview", "export", "index")
VERBS = {"find": "find", "search": "find", "show": "card", "card": "card", "walk": "walk", "read": "read", "tree": "tree", "links": "links",
         "sessions": "sessions", "projects": "projects", "live": "live", "skills": "skills", "overview": "overview", "export": "export", "dump": "export",
         "index": "index"}
INDEX_TIME_CAP = 8.0  # seconds spent catching up before a query proceeds anyway
_DURATION_RE = re.compile(r"^(\d+)\s*([hdwmy])$")


def parse_when(text: str | None, *, end_of_day: bool = False) -> float | None:
    """``24h 7d 2w 3m 1y`` relative, or ``YYYY-MM-DD`` / ISO absolute, to unix seconds."""
    if not text:
        return None
    text = text.strip()
    match = _DURATION_RE.match(text)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        seconds = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}[unit] * n
        return time.time() - seconds
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"bad time {text!r}: use 24h, 7d, 2w, 3m, 1y or YYYY-MM-DD") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    stamp = parsed.timestamp()
    if end_of_day and len(text) == 10:
        stamp += 86399
    return stamp


def parse_speakers(text: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not text:
        return default
    parts = tuple(p.strip().lower() for p in text.replace("+", ",").split(",") if p.strip())
    if "any" in parts or "all" in parts:
        return ("any",)
    bad = [p for p in parts if p not in ALL_SPEAKERS]
    if bad:
        raise SystemExit(f"unknown speaker {bad[0]!r}: use {', '.join(ALL_SPEAKERS)} or any")
    return parts


OPTIONS = {
    # name: (takes value, help)
    "ref": (True, GRAMMAR),
    "view": (True, "auto | " + " | ".join(v for v in VIEWS if v != "auto")),
    "store": (True, "omp | claude | cursor | hermes | hermes.<bank> | agy | all; comma-separated; default: every store not excluded in config"),
    "project": (True, "path prefix or folder name"),
    "speaker": (True, "user | assistant | tool | summary | system | thinking | mail | any; comma-separated"),
    "since": (True, "24h 7d 2w 3m 1y or YYYY-MM-DD"),
    "until": (True, "YYYY-MM-DD"),
    "limit": (True, "sessions for find, turns for walk, rows for listings and skills"),
    "from": (True, "first turn for walk"),
    "budget": (True, f"output ceiling in characters (default from config, max {MAX_BUDGET})"),
    "out": (True, "export target path"),
    "full": (False, "lift per-turn caps in read"),
    "no-fold": (False, "walk without folding tool runs"),
    "json": (False, "JSON envelope: schema, view, text, refs, meta"),
    "rebuild": (False, "with --view index: purge and rebuild every store"),
    "no-index": (False, "do not catch the index up before answering"),
}
INT_OPTIONS = {"limit", "from", "budget"}


def usage() -> str:
    lines = [__doc__.strip(), "", "options:"]
    for name, (takes, help_text) in OPTIONS.items():
        lines.append(f"  --{name}{' VALUE' if takes else '':<10} {help_text}")
    lines.append("  -h, --help")
    return "\n".join(lines)


class Args:
    """Parsed command line. Options may appear anywhere; ``--`` protects one following word."""

    def __init__(self, argv: list[str]) -> None:
        self.words: list[str] = []
        self.opts: dict[str, object] = {name: (None if takes else False) for name, (takes, _) in OPTIONS.items()}
        it = iter(argv)
        for arg in it:
            if not arg.startswith("-") or arg == "-":
                self.words.append(arg)
                continue
            if arg == "--":  # the next token is a word even if it starts with a dash (an excluded term like -html)
                nxt = next(it, None)
                if nxt is not None:
                    self.words.append(nxt)
                continue
            if arg in ("-h", "--help"):
                print(usage())
                raise SystemExit(0)
            name, eq, inline = arg[2:].partition("=") if arg.startswith("--") else (arg[1:], "", "")
            if name not in OPTIONS:
                raise SystemExit(f"unknown option {arg}; see --help")
            takes, _ = OPTIONS[name]
            if not takes:
                self.opts[name] = True
                continue
            value = inline if eq else next(it, None)
            if value is None:
                raise SystemExit(f"--{name} needs a value")
            if name in INT_OPTIONS:
                try:
                    value = int(value)
                except ValueError:
                    raise SystemExit(f"--{name} must be a number, got {value!r}") from None
            if name == "view" and value not in VIEWS:
                raise SystemExit(f"--view must be one of {', '.join(VIEWS)}")
            self.opts[name] = value

    def __getattr__(self, name: str):
        key = name.replace("_", "-")
        if key == "start":
            key = "from"
        try:
            return self.opts[key]
        except KeyError:
            raise AttributeError(name) from None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        args = Args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        print(exc, file=sys.stderr)
        return 2
    cfg = load_config()
    budget = max(200, min(MAX_BUDGET, args.budget or cfg.budget))
    words = list(args.words)
    view = args.view or "auto"
    ref_text = args.ref
    nullary = {"sessions", "projects", "live", "skills", "overview", "index"}
    if words and view == "auto" and words[0].lower() in VERBS and (len(words) >= 2 or words[0].lower() in nullary):
        view = VERBS[words.pop(0).lower()]
    if words and ref_text is None and len(words) == 1 and looks_like_ref(words[0]) and view in ("auto", "card", "walk", "read", "tree", "links", "export"):
        ref_text = words.pop(0)
    if words and ref_text is None and view in ("card", "walk", "read", "tree", "links", "export"):
        ref_text = words.pop(0)
    q = " ".join(words).strip()
    try:
        result = run(cfg, view=view, q=q, ref_text=ref_text, store=args.store, project=args.project, speaker=args.speaker, since=args.since,
                     until=args.until, limit=args.limit, start=args.start, full=args.full, fold=not args.no_fold, budget=budget, out_path=args.out,
                     rebuild=args.rebuild, reindex=not args.no_index)
    except RefError as exc:
        return _fail(str(exc), args.json)
    except SystemExit as exc:
        return _fail(str(exc), args.json)
    if args.json:
        print(json.dumps({"schema": 2, "view": result.kind, "text": result.text, "refs": result.refs, "meta": result.meta}, ensure_ascii=False))
    else:
        print(result.text)
    return 0


def _fail(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"schema": 2, "view": "error", "text": message, "refs": [], "meta": {}}, ensure_ascii=False))
    else:
        print(message, file=sys.stderr)
    return 2


def run(cfg, *, view: str, q: str, ref_text: str | None, store: str | None, project: str | None, speaker: str | None, since: str | None,
        until: str | None, limit: int | None, start: int | None, full: bool, fold: bool, budget: int, out_path: str | None,
        rebuild: bool, reindex: bool) -> View:
    from .adapters import discover_stores

    index = Index(cfg)
    try:
        all_stores = discover_stores(cfg)
        searched = _select_stores(cfg, all_stores, store)
        note = ""
        if view == "index":
            report = index.run(all_stores, rebuild=rebuild)
            lines = [f"index {'rebuilt' if rebuild else 'updated'} in {report.seconds:.1f}s: {report.sessions_seen} sessions seen, {report.threads_updated} threads read, +{report.turns_added} turns, {report.sessions_removed} removed"]
            lines.extend(f"  ! {e}" for e in report.errors[:10])
            renderer = Renderer(index, cfg)
            return View("index", "\n".join(lines) + "\n" + renderer.overview(budget, searched=tuple(s.name for s in searched)).text)
        if reindex:
            report = index.run(all_stores, time_cap=INDEX_TIME_CAP)
            if report.note():
                note = f"  ·  index: {report.note()}"
        renderer = Renderer(index, cfg)
        since_ts = parse_when(since)
        until_ts = parse_when(until, end_of_day=True)
        store_names = tuple(s.name for s in searched)
        excluded = [s.name for s in all_stores if s.exists and s.name not in store_names]
        scope_note = ""
        if store or excluded:
            scope_note = f"  ·  stores: {','.join(store_names)}" + (f" (not searched: {','.join(excluded)}; add store=all)" if excluded and not store else "")
        if project:
            scope_note += f"  ·  project: {project}"
        if since or until:
            scope_note += f"  ·  time: {since or ''}..{until or ''}"
        if q and looks_like_ref(q) and ref_text is None and view == "auto":
            ref_text, q = q, ""
        resolved = None
        if ref_text:
            try:
                resolved = index.resolve(parse_ref(ref_text))
            except RefError:
                # A token that looked like an id but is not one is probably a word: search for it.
                if view == "auto" and not q and " " not in ref_text and ":" not in ref_text and "@" not in ref_text:
                    q, ref_text = ref_text, None
                else:
                    raise
        elif q and " " not in q and view == "auto" and not project and not since:
            # A lone word that names a session prefix: show the session, not a search for its id.
            try:
                probe = index.resolve(parse_ref(q))
            except (RefError, ValueError):
                probe = None
            if probe is not None:
                resolved, q = probe, ""
        if view == "auto":
            if q:
                view = "find"
            elif resolved is None:
                view = "overview"
            elif resolved.turns is not None:
                view = "read"
            else:
                view = "card"
        if view == "overview":
            return renderer.overview(budget, searched=store_names)
        if view == "projects":
            return renderer.projects(index.projects(store_names, since_ts), budget)
        if view in ("sessions", "live"):
            rows = index.sessions(stores=store_names, project=project, since=since_ts, until=until_ts, limit=limit or 20, live_only=view == "live")
            title = ("active sessions (last 15 minutes)" if view == "live" else "sessions") + (scope_note or "") + f"  ·  {len(rows)} shown"
            return renderer.sessions(rows, budget, title=title, live=view == "live")
        if view == "skills":
            entries = index.skills(stores=store_names, project=project, since=since_ts, until=until_ts)
            return renderer.skills(entries[:limit] if limit else entries, budget, total=len(entries), total_uses=sum(e["uses"] for e in entries),
                                   since_ts=since_ts, until_ts=until_ts, stores=store_names, scope_note=scope_note + note)
        if view == "find":
            if not q:
                raise SystemExit("find needs words: scry WORDS...")
            speakers = parse_speakers(speaker, DEFAULT_SPEAKERS)
            scope = Scope(stores=store_names, project=project, since=since_ts, until=until_ts,
                          session_pk=resolved.session_pk if resolved else None,
                          thread_pk=resolved.thread_pk if resolved and resolved.thread_path else None)
            if resolved:
                scope_note += f"  ·  within {resolved.ref.thread()}"
            result = find(index, q, speakers=speakers, scope=scope, limit=limit or 8)
            v = renderer.find(result, budget, q=q, scope_note=scope_note + note, speakers=speakers)
            return v
        if resolved is None:
            raise SystemExit(f"{view} needs a ref. {GRAMMAR}")
        if view == "card":
            return renderer.card(resolved, budget)
        if view == "tree":
            return renderer.tree(resolved, budget)
        if view == "links":
            return renderer.links(resolved, budget)
        if view == "walk":
            speakers = parse_speakers(speaker, ()) or None
            return renderer.walk(resolved, budget, speakers=speakers, start=start, limit=limit or 60, fold=fold)
        if view == "read":
            speakers = parse_speakers(speaker, ()) or None
            if resolved.turns is None:
                resolved = index.resolve(parse_ref(f"{resolved.ref}@-3"))
            return renderer.read(resolved, budget, full=full, speakers=speakers)
        if view == "export":
            return renderer.export(resolved, out_path)
        raise SystemExit(f"unknown view {view}")
    finally:
        index.close()


def _select_stores(cfg, all_stores, store: str | None):
    existing = [s for s in all_stores if s.exists]
    if not store or store.strip().lower() == "default":
        return [s for s in existing if cfg.searched_by_default(s.name)]
    wanted = [w.strip() for w in store.split(",") if w.strip()]
    if "all" in wanted:
        return existing
    chosen = []
    for w in wanted:
        matches = [s for s in existing if s.name == w or s.name.startswith(w + ".") or s.harness == w]
        if not matches:
            raise SystemExit(f"no store {w!r}; stores here: {', '.join(s.name for s in existing) or 'none'}")
        chosen.extend(m for m in matches if m not in chosen)
    return chosen


if __name__ == "__main__":
    sys.exit(main())
