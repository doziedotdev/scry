"""One SQLite index over every store, incremental, derived and rebuildable.

The index is a cache of the transcripts. Refs never depend on its row ids, so it is
safe to delete ``~/.cache/scry/index.sqlite3`` and build it again.

Layout: ``store`` → ``session`` → ``thread`` → ``turn`` (+ ``turn_fts``), with a
``checkpoint`` per thread source so each pass reads only what changed. Session rows
are denormalized at index time (counts, tool histogram, files written, first user
turn, last assistant turn, latest summary) so a session card is one row read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterator

from .adapters.base import Checkpoint, SessionSource, StoreSpec, ThreadRead, ThreadSource
from .config import Config, ensure_private_dirs
from .gist import call_gist, call_paths, squash, summary_gist, task_gist, turn_gist
from .model import THREAD_KINDS, Call, Ref, RefError, SessionMeta, ThreadMeta, Turn, TurnRange
from .redact import redact

SCHEMA_VERSION = "4"
WRITE_TOOLS = {"write", "edit", "Write", "StrReplace", "Edit", "MultiEdit", "NotebookEdit", "ast_edit", "Delete", "create_file", "write_file", "edit_file", "apply_patch"}
LIVE_WINDOW = 15 * 60  # seconds since last activity that still counts as "active"
TOOL_SEARCH_CHARS = 8000  # how much of a tool result is searchable; all of it stays readable
PARALLEL_MIN = 8  # stale sessions before a pass fans out to worker processes

# Skill usage is read from what the index already holds, so no reindex is needed:
# an explicit invocation is an OMP ``[skill <name>]`` sub-turn or a Claude Code
# ``Skill`` call; a read is a file read of ``skill://<name>[/...]`` or of
# ``.../<name>/SKILL.md``, or a Hermes ``skill_view`` call.
SKILL_READ_TOOLS = {"read": ("path",), "read_file": ("path", "Path"), "Read": ("file_path",)}  # OMP, Hermes, Antigravity, Claude Code
SKILL_INVOKE_TOOLS = {"Skill"}  # Claude Code: {"skill": name}; older builds used {"command": name}
SKILL_VIEW_TOOLS = {"skill_view"}  # Hermes: {"name": name}
_SKILL_PROMPT = re.compile(r"^\[skill ([^\]\s]+)\]")
_SKILL_URL = re.compile(r"^skill://([^/:?#\s]+)")
_SKILL_FILE = re.compile(r"(?:^|/)([^/\s]+)/SKILL\.md(?::[^/]*)?$")


def workers() -> int:
    """Worker processes for a build: ``SCRY_JOBS`` or all but one core, at most 16."""
    override = os.environ.get("SCRY_JOBS")
    if override and override.isdigit():
        return max(1, int(override))
    return max(1, min(16, (os.cpu_count() or 2) - 1))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS store(
  name TEXT PRIMARY KEY, harness TEXT NOT NULL, location TEXT NOT NULL, indexed_at REAL
);
CREATE TABLE IF NOT EXISTS session(
  id INTEGER PRIMARY KEY,
  store TEXT NOT NULL, session_id TEXT NOT NULL,
  project TEXT, project_slug TEXT, title TEXT NOT NULL DEFAULT '',
  started REAL, ended REAL, last_ts REAL, activity REAL, status TEXT NOT NULL DEFAULT '',
  native_handle TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  turn_count INTEGER NOT NULL DEFAULT 0, thread_count INTEGER NOT NULL DEFAULT 1,
  summary TEXT NOT NULL DEFAULT '', summary_ordinal INTEGER,
  first_user TEXT NOT NULL DEFAULT '', first_user_ordinal INTEGER, first_user_sub INTEGER,
  last_user TEXT NOT NULL DEFAULT '', last_user_ordinal INTEGER, last_user_sub INTEGER,
  last_assistant TEXT NOT NULL DEFAULT '', last_assistant_ordinal INTEGER,
  speakers TEXT NOT NULL DEFAULT '{{}}', tools TEXT NOT NULL DEFAULT '{{}}', files TEXT NOT NULL DEFAULT '{{}}',
  artifacts TEXT NOT NULL DEFAULT '[]', extra TEXT NOT NULL DEFAULT '{{}}',
  indexed_at REAL NOT NULL,
  UNIQUE(store, session_id)
);
CREATE INDEX IF NOT EXISTS session_project ON session(project);
CREATE INDEX IF NOT EXISTS session_activity ON session(activity);
CREATE TABLE IF NOT EXISTS thread(
  id INTEGER PRIMARY KEY,
  session_pk INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  path TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
  agent_type TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  started REAL, ended REAL, status TEXT NOT NULL DEFAULT '', native_id TEXT,
  source TEXT NOT NULL, spawn_ordinal INTEGER, result_ordinal INTEGER,
  turn_count INTEGER NOT NULL DEFAULT 0, last_ts REAL,
  UNIQUE(session_pk, path)
);
CREATE TABLE IF NOT EXISTS turn(
  id INTEGER PRIMARY KEY,
  thread_pk INTEGER NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, ts REAL, speaker TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '', thinking TEXT NOT NULL DEFAULT '', calls TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '',
  calls_json TEXT NOT NULL DEFAULT '[]', tool TEXT, call_id TEXT, is_error INTEGER NOT NULL DEFAULT 0,
  gist TEXT NOT NULL DEFAULT '', chars INTEGER NOT NULL DEFAULT 0, native_id TEXT, offset INTEGER, sub INTEGER NOT NULL DEFAULT 0,
  UNIQUE(thread_pk, ordinal, sub)
);
-- FTS tokenizes turn_index.search_text: the whole text for people and summaries, a
-- bounded head of long tool output (command, file, error live at the top; a 150 KB dump
-- does not need every byte searchable). search_text is stored only when it differs from
-- text, and text itself is always complete for read and export.
CREATE VIEW IF NOT EXISTS turn_index AS
  SELECT id, CASE WHEN search_text <> '' THEN search_text ELSE text END AS search_text, thinking, calls FROM turn;
CREATE VIRTUAL TABLE IF NOT EXISTS turn_fts USING fts5(
  search_text, thinking, calls, content='turn_index', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS turn_ai AFTER INSERT ON turn BEGIN
  INSERT INTO turn_fts(rowid, search_text, thinking, calls)
    VALUES (new.id, CASE WHEN new.search_text <> '' THEN new.search_text ELSE new.text END, new.thinking, new.calls);
END;
CREATE TRIGGER IF NOT EXISTS turn_ad AFTER DELETE ON turn BEGIN
  INSERT INTO turn_fts(turn_fts, rowid, search_text, thinking, calls)
    VALUES ('delete', old.id, CASE WHEN old.search_text <> '' THEN old.search_text ELSE old.text END, old.thinking, old.calls);
END;
CREATE TABLE IF NOT EXISTS checkpoint(
  location TEXT PRIMARY KEY, thread_pk INTEGER NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0, ordinal INTEGER NOT NULL DEFAULT 0, native_id TEXT, guard TEXT NOT NULL DEFAULT '', sub INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memo(
  harness TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(harness, key)
);
"""


@dataclass(slots=True)
class IndexReport:
    stores: list[str] = field(default_factory=list)
    sessions_seen: int = 0
    threads_updated: int = 0
    turns_added: int = 0
    sessions_removed: int = 0
    behind: int = 0  # threads left unread because the time cap hit
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def note(self) -> str:
        parts = []
        if self.turns_added:
            parts.append(f"+{self.turns_added} turns")
        if self.behind:
            parts.append(f"{self.behind} threads behind, pass index to catch up")
        if self.errors:
            parts.append(f"{len(self.errors)} source errors")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class Resolved:
    """A ref bound to index rows. ``turns`` is the clamped inclusive range when one was given.

    ``turn_count`` counts integer turns only. ``sub`` is the ``k`` of a ``@N.k``
    selection (``turns`` is then ``(N, N)``); a bare ``@N`` selects the integer
    turn without its sub-turns, while ranges include the sub-turns inside them.
    """

    ref: Ref
    session_pk: int
    thread_pk: int
    thread_path: str
    turn_count: int
    turns: tuple[int, int] | None
    sub: int | None = None


class Memo:
    """Per-harness scratch the core keeps for adapters: what a source looked like at a fingerprint.

    Discovery must be cheap on unchanged sources. An adapter that has to parse a whole
    file to learn its structure (Claude sidechains, OMP session facts) stores the result
    here and gets it back for free until the file changes.
    """

    def __init__(self, db: sqlite3.Connection, harness: str) -> None:
        self.db = db
        self.harness = harness

    def get(self, key: str, fingerprint: str):
        row = self.db.execute("SELECT value FROM memo WHERE harness=? AND key=? AND fingerprint=?", (self.harness, key, fingerprint)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, key: str, fingerprint: str, value) -> None:
        self.db.execute(
            "INSERT INTO memo(harness, key, fingerprint, value) VALUES (?, ?, ?, ?) ON CONFLICT(harness, key) DO UPDATE SET fingerprint=excluded.fingerprint, value=excluded.value",
            (self.harness, key, fingerprint, json.dumps(value, ensure_ascii=False)),
        )


class Index:
    def __init__(self, cfg: Config, path: Path | None = None) -> None:
        self.cfg = cfg
        self.path = path or cfg.index_path
        ensure_private_dirs(cfg)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        self._specs: dict[str, StoreSpec] = {}

    # ------------------------------------------------------------------ schema

    def _ensure_schema(self) -> None:
        """Create or migrate the schema. On the common path (current version) this is one row read."""
        version = None
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = row[0] if row else None
        except sqlite3.DatabaseError:
            version = None
        if version == SCHEMA_VERSION:
            return
        if version is not None:
            self._drop_all()
        self.db.execute("PRAGMA journal_mode=WAL")  # persistent: set once at creation
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _drop_all(self) -> None:
        self.db.execute("DROP VIEW IF EXISTS turn_index")
        for name in ("checkpoint", "memo", "turn_fts", "turn", "thread", "session", "store", "meta"):
            self.db.execute(f"DROP TABLE IF EXISTS {name}")

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- indexing

    def run(self, stores: list[StoreSpec] | None = None, *, rebuild: bool = False, time_cap: float | None = None, keep_unlisted: bool = False) -> IndexReport:
        """Bring the index up to date. ``time_cap`` seconds stops early and reports ``behind``.

        ``stores`` is the complete configured set unless ``keep_unlisted`` is True: a store
        indexed earlier but absent from it was removed from the config and is dropped with
        everything under it.
        """
        from .adapters import discover_stores, get_adapter

        self.db.execute("PRAGMA synchronous=NORMAL")  # per connection; only the write path needs it
        started = time.monotonic()
        report = IndexReport()
        stores = stores if stores is not None else discover_stores(self.cfg)
        deadline = None if time_cap is None else started + time_cap
        for store in stores:
            if not store.exists:
                continue
            report.stores.append(store.name)
            failed = len(report.errors)  # a session that failed must be looked for again next pass
            adapter = get_adapter(store.harness)
            version = str(getattr(adapter, "version", ""))
            stale_reader = version != self._adapter_version(store.name)
            purge = rebuild or (stale_reader and bool(version))
            known = self._known_files(store.name)
            signature = store_signature(store, known)
            if not purge and signature and signature == self._store_signature(store.name):
                continue  # nothing under this store changed since the last pass
            roots = None if purge else self._changed_roots(store, known)
            adapter.memo = Memo(self.db, store.harness)
            self.db.execute(
                "INSERT INTO store(name, harness, location, indexed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET harness=excluded.harness, location=excluded.location, indexed_at=excluded.indexed_at",
                (store.name, store.harness, store.location, time.time()),
            )
            if purge:
                self.db.execute("DELETE FROM session WHERE store=?", (store.name,))
                # The saved state describes what was just deleted: left in place, a capped or
                # killed pass would look finished and the next one would skip the store.
                self.db.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (f"sig:{store.name}", f"files:{store.name}", f"stamps:{store.name}"))
            if version:  # saved now: a capped pass finishes by ordinary catch-up, not another purge
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"adapter:{store.name}", version))
            try:
                sources = _merge_duplicates(list(adapter.discover(store, roots)))
            except Exception as exc:  # a broken store must not hide the others
                report.errors.append(f"{store.name}: discover failed: {exc}")
                continue
            report.sessions_seen += len(sources)
            seen_ids = {s.session_id for s in sources}
            partial = roots is not None
            # Newest activity first, so live sessions catch up before old ones when capped.
            sources.sort(key=lambda s: max((_fingerprint_mtime(t.fingerprint) for t in s.threads), default=0.0), reverse=True)
            stale_sessions = [s for s in sources if any(self._needs_read(t) for t in s.threads) or self._session_pk(store.name, s.session_id) is None]
            if len(stale_sessions) >= PARALLEL_MIN and deadline is None and (jobs := workers()) > 1:
                self._run_parallel(adapter, store, stale_sessions, report, jobs)
            else:
                for source in stale_sessions:
                    if deadline is not None and time.monotonic() > deadline:
                        report.behind += sum(1 for t in source.threads if self._needs_read(t))
                        continue
                    try:
                        self._index_session(adapter, store, source, report)
                    except Exception as exc:
                        report.errors.append(f"{store.name}:{source.session_id}: {exc}")
            if partial:
                removed = 0  # a scoped discovery says nothing about sessions it did not visit
            elif seen_ids:
                placeholders = ",".join("?" * len(seen_ids))
                removed = self.db.execute(f"DELETE FROM session WHERE store=? AND session_id NOT IN ({placeholders})", (store.name, *seen_ids)).rowcount
            else:
                removed = self.db.execute("DELETE FROM session WHERE store=?", (store.name,)).rowcount
            report.sessions_removed += max(0, removed)
            if report.behind == 0 and len(report.errors) == failed:
                # Recompute after the pass: files that grew during it must not be marked done.
                self._specs[store.name] = store
                self._save_store_signature(store.name, self._query_known_files(store.name))
        if not keep_unlisted:
            self._drop_unconfigured_stores({store.name for store in stores}, report)
        report.seconds = time.monotonic() - started
        return report

    def _drop_unconfigured_stores(self, configured: set[str], report: IndexReport) -> None:
        """A store missing from the config is gone, not paused; a configured store whose
        location is missing for now (an unmounted mirror) stays, since it is still in ``configured``."""
        for (name,) in self.db.execute("SELECT name FROM store").fetchall():
            if name in configured:
                continue
            report.sessions_removed += max(0, self.db.execute("DELETE FROM session WHERE store=?", (name,)).rowcount)
            self.db.execute("DELETE FROM store WHERE name=?", (name,))
            self.db.execute("DELETE FROM meta WHERE key IN (?, ?)", (f"sig:{name}", f"adapter:{name}"))
            self._specs.pop(name, None)

    def _needs_read(self, thread: ThreadSource) -> bool:
        row = self.db.execute("SELECT fingerprint FROM checkpoint WHERE location=?", (thread.location,)).fetchone()
        return row is None or row[0] != thread.fingerprint

    def _adapter_version(self, name: str) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (f"adapter:{name}",)).fetchone()
        return row[0] if row else ""

    def _store_signature(self, name: str) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (f"sig:{name}",)).fetchone()
        return row[0] if row else ""

    def _changed_roots(self, store: StoreSpec, known: list[str]) -> set[str] | None:
        """Session roots whose files changed since the last pass, or None when a full discovery is needed.

        Only files the index already knows can be attributed to a root. A directory whose
        mtime moved may hold a brand-new session or agent file, so any directory change
        (other than a session's own artifact directory) forces full discovery.
        """
        if not os.path.isdir(store.location):
            return None
        stamps = self._file_stamps(store.name)
        if not stamps:
            return None
        changed: set[str] = set()
        for path in known:
            try:
                st = os.stat(path)
                stamp = f"{st.st_size}:{st.st_mtime_ns}"
            except OSError:
                return None  # a known file vanished: let full discovery prune it
            if stamps.get(path) != stamp:
                changed.add(path)
        dirs = {store.location}
        for path in known:
            parent = os.path.dirname(path)
            while parent.startswith(store.location) and parent not in dirs:
                dirs.add(parent)
                parent = os.path.dirname(parent)
        roots = set()
        for path in changed:
            root = self._root_of(store.name, path)
            if root is None:
                return None
            roots.add(root)
        for path in dirs:
            try:
                if stamps.get("dir:" + path) != str(os.stat(path).st_mtime_ns):
                    if store.harness == "agy":
                        return None  # AGY child subagents live in sibling brain/<child-id>/ dirs
                    owner = self._root_of_dir(store.name, path)
                    if owner is None:
                        return None  # a directory nobody owns changed: maybe a new session
                    roots.add(owner)  # a session's own directory changed: rediscover that session (new agent, new artifact)
            except OSError:
                return None
        if store.harness == "agy":
            return None if roots else set()
        return roots

    def _root_of_dir(self, store_name: str, directory: str) -> str | None:
        """The session whose artifact directory this is (OMP: ``<stamp>_<id>/``; Claude: ``<id>/``)."""
        row = self.db.execute(
            "SELECT se.source FROM session se WHERE se.store=? AND se.source = ? || '.jsonl' LIMIT 1", (store_name, directory),
        ).fetchone()
        if row:
            return row[0]
        # Nested agent directories (OMP: <session>/<Agent>/) belong to the session above them.
        parent = os.path.dirname(directory)
        row = self.db.execute(
            "SELECT se.source FROM session se WHERE se.store=? AND se.source = ? || '.jsonl' LIMIT 1", (store_name, parent),
        ).fetchone()
        return row[0] if row else None

    def _root_of(self, store_name: str, path: str) -> str | None:
        row = self.db.execute(
            "SELECT se.source FROM checkpoint c JOIN thread th ON th.id=c.thread_pk JOIN session se ON se.id=th.session_pk "
            "WHERE se.store=? AND (c.location=? OR c.location LIKE ?) LIMIT 1", (store_name, path, path + "#%"),
        ).fetchone()
        return row[0] if row else None

    def _file_stamps(self, name: str) -> dict[str, str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (f"stamps:{name}",)).fetchone()
        return json.loads(row[0]) if row else {}

    def _store_spec(self, name: str) -> StoreSpec:
        return self._specs[name]

    def _save_store_signature(self, name: str, known: list[str]) -> None:
        spec = self._store_spec(name)
        stamps: dict[str, str] = {}
        if os.path.isdir(spec.location):
            dirs = {spec.location}
            for path in known:
                try:
                    st = os.stat(path)
                    stamps[path] = f"{st.st_size}:{st.st_mtime_ns}"
                except OSError:
                    continue
                parent = os.path.dirname(path)
                while parent.startswith(spec.location) and parent not in dirs:
                    dirs.add(parent)
                    parent = os.path.dirname(parent)
            for path in dirs:
                try:
                    stamps["dir:" + path] = str(os.stat(path).st_mtime_ns)
                except OSError:
                    continue
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"files:{name}", json.dumps(known)))
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"stamps:{name}", json.dumps(stamps)))
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"sig:{name}", store_signature(spec, known)))

    def _known_files(self, name: str) -> list[str]:
        """Every source file the index has read for this store, cached beside the signature.

        Stat-ing thousands of files costs milliseconds; the three-way join to list them
        costs more than that, so the list is stored when the signature is saved.
        """
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (f"files:{name}",)).fetchone()
        if row is not None:
            return json.loads(row[0])
        return self._query_known_files(name)

    def _query_known_files(self, name: str) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT c.location FROM checkpoint c JOIN thread th ON th.id=c.thread_pk JOIN session se ON se.id=th.session_pk WHERE se.store=?",
            (name,),
        ).fetchall()
        return sorted({r[0].partition("#")[0] for r in rows if not r[0].startswith("sqlite:")})

    def _index_session(self, adapter, store: StoreSpec, source: SessionSource, report: IndexReport) -> None:
        stale = [t for t in source.threads if self._needs_read(t)]
        session_pk = self._session_pk(store.name, source.session_id)
        if not stale and session_pk is not None:
            return
        checkpoints = {t.location: self._checkpoint(t.location) for t in stale}
        prepared = prepare_session(adapter, source, stale, checkpoints)
        self._commit_session(store, source, prepared, report)

    def _run_parallel(self, adapter, store: StoreSpec, sessions: list[SessionSource], report: IndexReport, jobs: int) -> None:
        """Workers read and build rows; this process commits them in arrival order.

        Biggest sessions are submitted first so no single large file is left running
        alone at the end. Workers have no database, so their memo writes are lost; the
        next serial discovery refills the memo.
        """
        import multiprocessing as mp

        by_id = {s.session_id: s for s in sessions}
        jobs_list = []
        for source in sorted(sessions, key=lambda s: -sum(_fingerprint_size(t.fingerprint) for t in s.threads)):
            stale = [t for t in source.threads if self._needs_read(t)]
            checkpoints = {t.location: self._checkpoint(t.location) for t in stale}
            jobs_list.append((store.harness, source, stale, checkpoints))
        ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context("spawn")
        self.db.commit() if self.db.in_transaction else None
        with ctx.Pool(processes=jobs) as pool:
            for session_id, prepared, error in pool.imap_unordered(_prepare_in_worker, jobs_list, chunksize=1):
                if prepared is None:
                    report.errors.append(f"{store.name}:{session_id}: {error}")
                    continue
                try:
                    self._commit_session(store, by_id[session_id], prepared, report)
                except Exception as exc:
                    report.errors.append(f"{store.name}:{session_id}: {exc}")

    def _commit_session(self, store: StoreSpec, source: SessionSource, prepared: "PreparedSession", report: IndexReport) -> None:
        """Apply one prepared session: the only place turns are written."""
        session_pk = self._session_pk(store.name, source.session_id)
        stale_locations = {t.location for t, _, _ in prepared.threads}
        self.db.execute("BEGIN")
        try:
            session_pk = self._upsert_session(prepared.meta, session_pk)
            changed = False
            for thread in source.threads:
                if thread.location not in stale_locations and self._thread_pk(session_pk, thread.path) is not None:
                    continue
                read, rows = next(((r, rw) for t, r, rw in prepared.threads if t.location == thread.location), (None, None))
                if read is None:  # not stale but missing from the index: read it now
                    from .adapters import get_adapter

                    read = get_adapter(store.harness).read_thread(source, thread, None)
                    rows = [self._turn_row(0, turn) for turn in read.turns]
                if read.reset:
                    self._purge_thread(session_pk, thread.path)
                added = self._apply_thread(session_pk, thread, read, rows)
                report.turns_added += added
                report.threads_updated += 1
                changed = changed or added > 0 or read.reset
            live_paths = {"/".join(t.path) for t in source.threads}
            for row in self.db.execute("SELECT id, path FROM thread WHERE session_pk=?", (session_pk,)).fetchall():
                if row["path"] not in live_paths:
                    self.db.execute("DELETE FROM thread WHERE id=?", (row["id"],))
                    changed = True
            if changed or not prepared.threads:
                self._refresh_session(session_pk, prepared.meta)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _session_pk(self, store: str, session_id: str) -> int | None:
        row = self.db.execute("SELECT id FROM session WHERE store=? AND session_id=?", (store, session_id)).fetchone()
        return None if row is None else int(row[0])

    def _thread_pk(self, session_pk: int, path: tuple[str, ...]) -> int | None:
        row = self.db.execute("SELECT id FROM thread WHERE session_pk=? AND path=?", (session_pk, "/".join(path))).fetchone()
        return None if row is None else int(row[0])

    def _checkpoint(self, location: str) -> Checkpoint | None:
        row = self.db.execute("SELECT fingerprint, offset, ordinal, native_id, guard, sub FROM checkpoint WHERE location=?", (location,)).fetchone()
        if row is None:
            return None
        return Checkpoint(row["fingerprint"], int(row["offset"]), int(row["ordinal"]), row["native_id"], row["guard"], int(row["sub"] or 0))

    def _purge_thread(self, session_pk: int, path: tuple[str, ...]) -> None:
        self.db.execute("DELETE FROM thread WHERE session_pk=? AND path=?", (session_pk, "/".join(path)))

    def _upsert_session(self, meta: SessionMeta, session_pk: int | None) -> int:
        """Store the session row. ``project_slug`` is always set: it is the cross-harness pivot.

        Harnesses that record a working directory get its slug computed here; harnesses
        that only have a slug keep theirs. Two sessions of one project then match on the
        slug on any machine, while the decoded ``project`` path stays a display and
        filter convenience that may depend on which directories exist locally.
        """
        now = time.time()
        meta = replace(meta, project_slug=project_slug(meta.project, meta.project_slug))
        if session_pk is None:
            cur = self.db.execute(
                "INSERT INTO session(store, session_id, project, project_slug, title, started, ended, status, native_handle, source, model, artifacts, extra, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (meta.store, meta.session_id, meta.project, meta.project_slug, redact(meta.title), meta.started, meta.ended, meta.status,
                 meta.native_handle, meta.source, meta.model, json.dumps([asdict(a) for a in meta.artifacts]), json.dumps(meta.extra), now),
            )
            return int(cur.lastrowid)
        self.db.execute(
            "UPDATE session SET project=?, project_slug=?, title=?, started=?, ended=?, status=?, native_handle=?, source=?, model=?, artifacts=?, extra=?, indexed_at=? WHERE id=?",
            (meta.project, meta.project_slug, redact(meta.title), meta.started, meta.ended, meta.status, meta.native_handle, meta.source, meta.model,
             json.dumps([asdict(a) for a in meta.artifacts]), json.dumps(meta.extra), now, session_pk),
        )
        return session_pk

    def _apply_thread(self, session_pk: int, thread: ThreadSource, read: ThreadRead, rows: list[tuple]) -> int:
        meta = read.meta
        path = "/".join(meta.path)
        thread_pk = self._thread_pk(session_pk, meta.path)
        if thread_pk is None:
            cur = self.db.execute(
                "INSERT INTO thread(session_pk, path, kind, label, agent_type, task, model, started, ended, status, native_id, source, spawn_ordinal, result_ordinal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_pk, path, meta.kind, meta.label or (meta.path[-1] if meta.path else "main"), meta.agent_type, redact(task_gist(meta.task)),
                 meta.model, meta.started, meta.ended, meta.status, meta.native_id, meta.source or thread.location, meta.spawn_ordinal, meta.result_ordinal),
            )
            thread_pk = int(cur.lastrowid)
        else:
            self.db.execute(
                "UPDATE thread SET kind=?, label=?, agent_type=?, task=?, model=?, started=?, ended=?, status=?, native_id=?, source=?, spawn_ordinal=?, result_ordinal=? WHERE id=?",
                (meta.kind, meta.label or (meta.path[-1] if meta.path else "main"), meta.agent_type, redact(task_gist(meta.task)), meta.model, meta.started,
                 meta.ended, meta.status, meta.native_id, meta.source or thread.location, meta.spawn_ordinal, meta.result_ordinal, thread_pk),
            )
        rows = [(thread_pk, *row[1:]) for row in rows]
        if rows:
            self.db.executemany(
                "INSERT OR REPLACE INTO turn(thread_pk, ordinal, ts, speaker, text, thinking, calls, search_text, calls_json, tool, call_id, is_error, gist, chars, native_id, offset, sub) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        stats = self.db.execute("SELECT SUM(CASE WHEN sub = 0 THEN 1 ELSE 0 END) AS n, MAX(ts) AS last_ts FROM turn WHERE thread_pk=?", (thread_pk,)).fetchone()
        self.db.execute("UPDATE thread SET turn_count=?, last_ts=? WHERE id=?", (int(stats["n"] or 0), stats["last_ts"], thread_pk))
        cp = read.checkpoint
        self.db.execute(
            "INSERT INTO checkpoint(location, thread_pk, fingerprint, offset, ordinal, native_id, guard, sub) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(location) DO UPDATE SET thread_pk=excluded.thread_pk, fingerprint=excluded.fingerprint, offset=excluded.offset, ordinal=excluded.ordinal, native_id=excluded.native_id, guard=excluded.guard, sub=excluded.sub",
            (thread.location, thread_pk, thread.fingerprint, cp.offset, cp.ordinal, cp.native_id, cp.guard, cp.sub),
        )
        return len(rows)

    def _turn_row(self, thread_pk: int, turn: Turn) -> tuple:
        return turn_row(thread_pk, turn)

    def _refresh_session(self, session_pk: int, meta: SessionMeta) -> None:
        """Recompute the denormalized card fields from the main thread and all threads."""
        main = self.db.execute("SELECT id FROM thread WHERE session_pk=? AND path=''", (session_pk,)).fetchone()
        speakers: Counter[str] = Counter()
        tools: Counter[str] = Counter()
        written: list[str] = []
        read_paths: list[str] = []
        first_user = last_user = last_assistant = summary = ""
        first_user_ord = last_user_ord = last_assistant_ord = summary_ord = None
        first_user_sub = last_user_sub = 0
        if main is not None:
            for row in self.db.execute(
                "SELECT ordinal, sub, speaker, gist, calls_json, text FROM turn WHERE thread_pk=? ORDER BY ordinal, sub", (main["id"],)
            ):
                speakers[row["speaker"]] += 1
                if row["speaker"] == "user":
                    if first_user_ord is None:
                        first_user, first_user_ord, first_user_sub = row["gist"], row["ordinal"], row["sub"]
                    last_user, last_user_ord, last_user_sub = row["gist"], row["ordinal"], row["sub"]
                elif row["speaker"] == "assistant":
                    last_assistant, last_assistant_ord = squash(row["text"], 400) or row["gist"], row["ordinal"]
                elif row["speaker"] == "summary":
                    summary, summary_ord = summary_gist(row["text"], 600), row["ordinal"]
                if row["calls_json"] != "[]":
                    for call in json.loads(row["calls_json"]):
                        tools[call["name"]] += 1
                        writing = call["name"] in WRITE_TOOLS
                        target = written if writing else read_paths
                        for p in call_paths(_as_call(call), explicit_only=writing):
                            if p not in target:
                                target.append(p)
        totals = self.db.execute(
            "SELECT SUM(CASE WHEN t.id IS NOT NULL AND t.sub = 0 THEN 1 ELSE 0 END) AS turns, COUNT(DISTINCT CASE WHEN th.kind <> 'workflow' THEN th.id END) AS threads, MAX(t.ts) AS last_ts FROM thread th LEFT JOIN turn t ON t.thread_pk=th.id WHERE th.session_pk=?",
            (session_pk,),
        ).fetchone()
        act_ts = max(filter(None, (_fingerprint_mtime(row[0]) for row in self.db.execute(
            "SELECT c.fingerprint FROM checkpoint c JOIN thread th ON th.id=c.thread_pk WHERE th.session_pk=?", (session_pk,)))), default=None)
        self.db.execute(
            "UPDATE session SET turn_count=?, thread_count=?, last_ts=?, activity=?, summary=?, summary_ordinal=?, first_user=?, first_user_ordinal=?, first_user_sub=?, "
            "last_user=?, last_user_ordinal=?, last_user_sub=?, last_assistant=?, last_assistant_ordinal=?, speakers=?, tools=?, files=? WHERE id=?",
            (int(totals["turns"] or 0), int(totals["threads"]), totals["last_ts"], act_ts or totals["last_ts"] or meta.ended or meta.started,
             summary, summary_ord, first_user, first_user_ord, first_user_sub, last_user, last_user_ord, last_user_sub, last_assistant, last_assistant_ord,
             json.dumps(dict(speakers)), json.dumps(dict(tools.most_common())), json.dumps({"written": written[:40], "read": read_paths[:60]}), session_pk),
        )

    # -------------------------------------------------------------- resolution

    def resolve(self, ref: Ref) -> Resolved:
        """Bind a ref (possibly a prefix, possibly store-less) to index rows or raise ``RefError``."""
        params: list = []
        where = "session_id = ? OR session_id LIKE ? ESCAPE '\\'"
        params.extend([ref.session, like_prefix(ref.session)])
        if ref.store:
            where = f"store = ? AND ({where})"
            params.insert(0, ref.store)
        rows = self.db.execute(f"SELECT id, store, session_id FROM session WHERE {where} ORDER BY activity DESC LIMIT 12", params).fetchall()
        exact = [r for r in rows if r["session_id"] == ref.session and (ref.store is None or r["store"] == ref.store)]
        if len(exact) == 1:
            rows = exact
        if not rows:
            raise RefError(self._nearest_message(ref))
        if len(rows) > 1:
            options = ", ".join(f"{r['store']}:{_short(r['session_id'])}" for r in rows[:8])
            raise RefError(f"{ref} matches {len(rows)} sessions; be more specific: {options}")
        session_pk = int(rows[0]["id"])
        bound = Ref(rows[0]["store"], rows[0]["session_id"], ref.agent, ref.turns)
        path = "/".join(ref.agent)
        thread = self.db.execute("SELECT id, path, turn_count FROM thread WHERE session_pk=? AND path=?", (session_pk, path)).fetchone()
        if thread is None and ref.agent:
            candidates = self.db.execute(
                "SELECT id, path, turn_count, kind FROM thread WHERE session_pk=? AND (lower(path) = lower(?) OR path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\') ORDER BY path",
                (session_pk, path, like_prefix(path), "%/" + like_escape(path) + "%"),
            ).fetchall()
            if len(candidates) == 1:
                thread = candidates[0]
                bound = Ref(bound.store, bound.session, tuple(thread["path"].split("/")), ref.turns)
            elif parents := [c for c in candidates if c["kind"] == "workflow" and all(
                    o["path"] == c["path"] or o["path"].startswith(c["path"] + "/") for o in candidates)]:
                thread = parents[0]  # a prefix of a workflow run also matches the agents below it
                bound = Ref(bound.store, bound.session, tuple(thread["path"].split("/")), ref.turns)
            elif candidates:
                raise RefError(f"{ref}: agent {path!r} is ambiguous: " + ", ".join(f"{bound.root()}/{c['path']}" for c in candidates[:8]))
            else:
                have = [r["path"] for r in self.db.execute("SELECT path FROM thread WHERE session_pk=? AND path<>'' ORDER BY path", (session_pk,))]
                hint = ("agents here: " + ", ".join(have[:10])) if have else "this session has no child agents"
                raise RefError(f"{ref}: no agent {path!r}; {hint}")
        if thread is None:
            raise RefError(f"{ref}: session has no main thread indexed yet; run index")
        count = int(thread["turn_count"])
        turns = None
        sub = None
        if ref.turns is not None:
            if ref.turns.sub is not None:
                have = self.db.execute(
                    "SELECT MAX(sub) FROM turn WHERE thread_pk=? AND ordinal=?", (int(thread["id"]), ref.turns.start),
                ).fetchone()[0] or 0
                if ref.turns.sub > have:
                    if have == 0:
                        raise RefError(f"{bound.thread()}@{ref.turns.start} has no sub-turns")
                    raise RefError(f"{bound.thread()}@{ref.turns.start} has sub-turns 1-{have}; @{ref.turns} selects none")
                turns = (ref.turns.start, ref.turns.start)
                sub = ref.turns.sub
            else:
                first, last = ref.turns.resolve(count)
                if first > last:
                    raise RefError(f"{bound.thread()} has {count} turns; {ref.turns} selects none")
                turns = (first, last)
        return Resolved(bound, session_pk, int(thread["id"]), thread["path"], count, turns, sub)

    def _nearest_message(self, ref: Ref) -> str:
        prefix = ref.session[:4]
        rows = self.db.execute(
            "SELECT store, session_id FROM session WHERE session_id LIKE ? ESCAPE '\\' ORDER BY activity DESC LIMIT 6", (like_prefix(prefix),)
        ).fetchall()
        stores = [r[0] for r in self.db.execute("SELECT name FROM store ORDER BY name")]
        if rows:
            return f"no session {ref}; nearest ids: " + ", ".join(f"{r['store']}:{_short(r['session_id'])}" for r in rows)
        scope = f" in store {ref.store}" if ref.store else ""
        return f"no session {ref}{scope}. Stores indexed: {', '.join(stores) or 'none (run index)'}"

    # ------------------------------------------------------------------ reads

    def session_row(self, session_pk: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM session WHERE id=?", (session_pk,)).fetchone()

    def thread_rows(self, session_pk: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM thread WHERE session_pk=? ORDER BY path", (session_pk,)).fetchall()

    def thread_row(self, thread_pk: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM thread WHERE id=?", (thread_pk,)).fetchone()

    def turns(self, thread_pk: int, first: int, last: int, *, speakers: tuple[str, ...] | None = None, subs: bool = True) -> list[sqlite3.Row]:
        # Sub-turns before the first integer turn (@0.k) belong to the start of the
        # thread: any span starting at 1 shows them, so full walks and exports lose nothing.
        lo = 0 if first <= 1 else first
        sql = "SELECT * FROM turn WHERE thread_pk=? AND ordinal BETWEEN ? AND ?"
        params: list = [thread_pk, lo, last]
        if speakers:
            sql += " AND speaker IN (%s)" % ",".join("?" * len(speakers))
            params.extend(speakers)
        if not subs:
            sql += " AND sub = 0"
        return self.db.execute(sql + " ORDER BY ordinal, sub", params).fetchall()

    def turn(self, thread_pk: int, ordinal: int, sub: int = 0) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM turn WHERE thread_pk=? AND ordinal=? AND sub=?", (thread_pk, ordinal, sub)).fetchone()

    def stores(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT s.name, s.harness, s.location, s.indexed_at, COUNT(se.id) AS sessions, COALESCE(SUM(se.turn_count),0) AS turns, "
            "MIN(se.started) AS first_ts, MAX(se.activity) AS last_ts FROM store s LEFT JOIN session se ON se.store=s.name GROUP BY s.name ORDER BY s.name"
        ).fetchall()

    def projects(self, stores: tuple[str, ...] | None = None, since: float | None = None) -> list[sqlite3.Row]:
        sql = "SELECT COALESCE(project, project_slug, '(unknown)') AS project, COUNT(*) AS sessions, SUM(turn_count) AS turns, MIN(started) AS first_ts, MAX(activity) AS last_ts, GROUP_CONCAT(DISTINCT store) AS stores FROM session"
        clauses, params = _scope(stores, since, None)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.db.execute(sql + " GROUP BY 1 ORDER BY last_ts DESC", params).fetchall()

    def sessions(self, *, stores: tuple[str, ...] | None, project: str | None, since: float | None, until: float | None, limit: int, live_only: bool = False) -> list[sqlite3.Row]:
        clauses, params = _scope(stores, since, until)
        _project_clause(project, clauses, params)
        if live_only:
            clauses.append("activity >= ?")
            params.append(time.time() - LIVE_WINDOW)
        sql = "SELECT * FROM session"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.db.execute(sql + " ORDER BY activity DESC LIMIT ?", (*params, limit)).fetchall()

    def skills(self, *, stores: tuple[str, ...] | None, project: str | None, since: float | None, until: float | None) -> list[dict]:
        """How often each skill was used: one entry per skill, most used first.

        Each entry: ``name uses invoked read sessions threads first_ts last_ts`` plus
        ``last`` = ``(store, session_id, thread_path, ordinal, sub)`` of the newest use,
        which the view turns into a ref. A use is timed by its turn, so ``since`` and
        ``until`` bound the uses themselves, not only the sessions holding them.
        """
        clauses, params = _scope(stores, since, until, prefix="se.")
        _project_clause(project, clauses, params, prefix="se.")
        when = "COALESCE(t.ts, se.activity, se.started)"
        if since is not None:
            clauses.append(f"{when} >= ?")
            params.append(since)
        if until is not None:
            clauses.append(f"{when} <= ?")
            params.append(until)
        scope = "".join(f" AND {c}" for c in clauses)
        head = f"SELECT se.store, se.session_id, th.path, th.kind, t.ordinal, t.sub, {when} AS ts"
        joins = "FROM turn t JOIN thread th ON th.id=t.thread_pk JOIN session se ON se.id=th.session_pk"
        uses: list[tuple[str, str, sqlite3.Row]] = []
        for row in self.db.execute(f"{head}, t.text {joins} WHERE t.sub > 0 AND t.text LIKE '[skill %'{scope}", params):
            match = _SKILL_PROMPT.match(row["text"])
            if match and match.group(1) != "?":
                uses.append((match.group(1), "invoked", row))
        names = sorted({*SKILL_READ_TOOLS, *SKILL_INVOKE_TOOLS, *SKILL_VIEW_TOOLS})
        tool_sql = (f"{head}, json_extract(c.value, '$.name') AS tool, json_extract(c.value, '$.args') AS args {joins}, json_each(t.calls_json) c "
                    f"WHERE t.calls_json LIKE '%skill%' AND json_extract(c.value, '$.name') IN ({','.join('?' * len(names))}){scope}")
        for row in self.db.execute(tool_sql, (*names, *params)):
            found = skill_of_call(row["tool"], row["args"])
            if found:
                uses.append((*found, row))
        stats: dict[str, dict] = {}
        for name, how, row in uses:
            entry = stats.get(name)
            if entry is None:
                entry = stats[name] = {"name": name, "uses": 0, "invoked": 0, "read": 0, "sessions": set(), "threads": dict.fromkeys((k for k in THREAD_KINDS if k != "workflow"), 0),
                                       "first_ts": None, "last_ts": None, "last": None}
            entry["uses"] += 1
            entry[how] += 1
            entry["sessions"].add((row["store"], row["session_id"]))
            entry["threads"][row["kind"]] = entry["threads"].get(row["kind"], 0) + 1
            ts = row["ts"]
            if ts is not None and (entry["first_ts"] is None or ts < entry["first_ts"]):
                entry["first_ts"] = ts
            if entry["last"] is None or (ts or 0.0) >= (entry["last_ts"] or 0.0):
                entry["last_ts"] = ts if ts is not None else entry["last_ts"]
                entry["last"] = (row["store"], row["session_id"], row["path"], row["ordinal"], row["sub"])
        for entry in stats.values():
            entry["sessions"] = len(entry["sessions"])
        return sorted(stats.values(), key=lambda e: (-e["uses"], e["name"]))

    def mentions(self, session_id: str, *, exclude_pk: int, limit: int = 8) -> list[sqlite3.Row]:
        """Sessions whose text names this session id: who looked at it, who handed it over."""
        token = session_id[:12]
        return self.db.execute(
            "SELECT DISTINCT se.id, se.store, se.session_id, se.activity, th.path, t.ordinal, t.sub FROM turn_fts f "
            "JOIN turn t ON t.id=f.rowid JOIN thread th ON th.id=t.thread_pk JOIN session se ON se.id=th.session_pk "
            "WHERE turn_fts MATCH ? AND se.id<>? ORDER BY se.activity DESC LIMIT ?",
            (f'"{token}"', exclude_pk, limit),
        ).fetchall()

    def neighbours(self, session_pk: int, *, limit: int = 6) -> list[sqlite3.Row]:
        """Sessions in other stores with the same project (by slug) and overlapping time."""
        row = self.session_row(session_pk)
        if row is None or not row["project_slug"]:
            return []
        start = (row["started"] or row["activity"] or 0) - 3600
        end = (row["activity"] or row["ended"] or row["started"] or 0) + 3600
        return self.db.execute(
            "SELECT * FROM session WHERE id<>? AND project_slug=? AND COALESCE(activity, started) BETWEEN ? AND ? ORDER BY abs(COALESCE(started,0) - ?) LIMIT ?",
            (session_pk, row["project_slug"], start, end, row["started"] or 0, limit),
        ).fetchall()


# ---------------------------------------------------------------------- utils


def _scope(stores: tuple[str, ...] | None, since: float | None, until: float | None, *, prefix: str = "") -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    if stores:
        clauses.append(f"{prefix}store IN (%s)" % ",".join("?" * len(stores)))
        params.extend(stores)
    if since is not None:
        clauses.append(f"COALESCE({prefix}activity, {prefix}started) >= ?")
        params.append(since)
    if until is not None:
        clauses.append(f"COALESCE({prefix}started, {prefix}activity) <= ?")
        params.append(until)
    return clauses, params


def _project_clause(project: str | None, clauses: list[str], params: list, *, prefix: str = "") -> None:
    """A project filter: a path prefix, a slug fragment, or a folder name."""
    if project:
        clauses.append(f"({prefix}project LIKE ? OR {prefix}project_slug LIKE ? OR {prefix}project LIKE ?)")
        params.extend([project + "%", "%" + project + "%", "%/" + project.strip("/") + "%"])


def skill_of_call(tool: str, args: str) -> tuple[str, str] | None:
    """``(skill, "invoked" | "read")`` when a tool call used a skill, else None."""
    if tool not in SKILL_READ_TOOLS and tool not in SKILL_INVOKE_TOOLS and tool not in SKILL_VIEW_TOOLS:
        return None
    try:
        data = json.loads(args) if args else {}
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if tool in SKILL_INVOKE_TOOLS:
        name = data.get("skill") or data.get("command")
        name = name.strip().lstrip("/") if isinstance(name, str) else ""
        return (name, "invoked") if name else None
    if tool in SKILL_VIEW_TOOLS:
        name = data.get("name")
        return (name.strip(), "read") if isinstance(name, str) and name.strip() else None
    path = next((data.get(k) for k in SKILL_READ_TOOLS[tool] if isinstance(data.get(k), str)), None)
    if not isinstance(path, str):
        return None
    path = path.strip().replace("\\", "/")
    match = _SKILL_URL.match(path) or _SKILL_FILE.search(path)
    return (match.group(1), "read") if match else None


def _fingerprint_mtime(fingerprint: str) -> float:
    """``size:mtime_ns`` fingerprints carry the source mtime; others do not."""
    _, _, tail = fingerprint.partition(":")
    if fingerprint.startswith("maxid") or not tail.isdigit():
        return 0.0
    value = int(tail)
    return value / 1e9 if value > 1e12 else float(value)


def _short(session_id: str) -> str:
    return session_id[:8] if len(session_id) > 12 else session_id


def _as_call(item: dict):
    from .model import Call

    return Call(item.get("name", ""), item.get("args", ""), item.get("intent", ""), item.get("id"))


def _merge_duplicates(sources: list[SessionSource]) -> list[SessionSource]:
    """One SessionSource per session id.

    Claude writes a one-record stub of a session into a worktree's project directory
    when the working directory moves. Two sources for one id would delete each other's
    threads on alternate passes, so the source with the largest main file wins and the
    other sources contribute only threads whose paths it lacks.
    """
    by_id: dict[str, list[SessionSource]] = {}
    for source in sources:
        by_id.setdefault(source.session_id, []).append(source)
    merged: list[SessionSource] = []
    for items in by_id.values():
        if len(items) == 1:
            merged.append(items[0])
            continue
        items.sort(key=lambda s: _fingerprint_size(next((t.fingerprint for t in s.threads if t.path == ()), "0")), reverse=True)
        primary = items[0]
        threads = list(primary.threads)
        paths = {t.path for t in threads}
        for other in items[1:]:
            for thread in other.threads:
                if thread.path not in paths:
                    threads.append(thread)
                    paths.add(thread.path)
        merged.append(SessionSource(primary.store, primary.session_id, primary.root, tuple(threads)))
    return merged


def _fingerprint_size(fingerprint: str) -> int:
    head, _, _ = fingerprint.partition(":")
    return int(head) if head.isdigit() else 0


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_prefix(text: str) -> str:
    return like_escape(text) + "%"


def project_slug(project: str | None, given: str | None = None) -> str | None:
    """The Claude style directory name for a working directory: ``/home/me/work`` → ``-home-me-work``.

    A harness-supplied slug wins but is normalized to the leading-dash form, so Cursor's
    ``home-me-work`` and Claude's ``-home-me-work`` pivot together.
    """
    if given:
        return given if given.startswith("-") else "-" + given
    if not project:
        return None
    return "-" + project.strip("/").replace("/", "-")


# --------------------------------------------------------------- preparation
#
# Everything below is pure: it reads transcripts and builds rows, and touches no
# database. That is what lets a build fan out across processes while the writer
# stays single and simple.


@dataclass(slots=True)
class PreparedSession:
    meta: SessionMeta
    threads: list[tuple[ThreadSource, ThreadRead, list[tuple]]]  # stale threads: read plus prebuilt rows (thread_pk 0)


def turn_row(thread_pk: int, turn: Turn) -> tuple:
    text = redact(turn.text)
    thinking = redact(turn.thinking)
    calls = []
    calls_text: list[str] = []
    redacted_calls = []
    for call in turn.calls:
        args = redact(call.args)
        calls.append({"name": call.name, "intent": call.intent, "args": args, "id": call.call_id})
        calls_text.append(" ".join(part for part in (call.name, call.intent, args) if part))
        redacted_calls.append(Call(call.name, args, call.intent, call.call_id))
    redacted = Turn(turn.ordinal, turn.speaker, text, thinking, tuple(redacted_calls), turn.ts, turn.tool, turn.call_id, turn.is_error, turn.native_id, turn.offset, turn.sub)
    gist = turn_gist(redacted)
    search_text = text[:TOOL_SEARCH_CHARS] if turn.speaker == "tool" and len(text) > TOOL_SEARCH_CHARS else ""
    return (
        thread_pk, turn.ordinal, turn.ts, turn.speaker, text, thinking, "\n".join(calls_text), search_text, json.dumps(calls, ensure_ascii=False),
        turn.tool, turn.call_id, 1 if turn.is_error else 0, gist, len(text) + len(thinking), turn.native_id, turn.offset, turn.sub,
    )


def prepare_session(adapter, source: SessionSource, stale: list[ThreadSource], checkpoints: dict[str, Checkpoint | None]) -> PreparedSession:
    meta = adapter.session_meta(source)
    threads = []
    for thread in stale:
        read = adapter.read_thread(source, thread, checkpoints.get(thread.location))
        if read.reset:
            read = adapter.read_thread(source, thread, None)
            read.reset = True  # the writer purges before applying
        threads.append((thread, read, [turn_row(0, turn) for turn in read.turns]))
    return PreparedSession(meta, threads)


_worker_adapters: dict[str, object] = {}


def _prepare_in_worker(job: tuple) -> tuple[str, "PreparedSession | None", str]:
    """Pool entry point: ``(harness, source, stale, checkpoints)`` → ``(session_id, prepared, error)``."""
    harness, source, stale, checkpoints = job
    adapter = _worker_adapters.get(harness)
    if adapter is None:
        from .adapters import get_adapter

        adapter = _worker_adapters[harness] = get_adapter(harness)
    try:
        return source.session_id, prepare_session(adapter, source, stale, checkpoints), ""
    except Exception as exc:  # reported per session, never fatal to the pass
        return source.session_id, None, f"{type(exc).__name__}: {exc}"


def store_signature(store: StoreSpec, known_files: list[str]) -> str:
    """A cheap fingerprint of everything that could have changed in a store.

    A directory's mtime changes when a file or directory is added, removed or renamed
    directly inside it, so stat-ing the store root, every directory on the way down to a
    known file, and every known file catches new sessions, new agents, growth and
    truncation. A brand-new subtree bumps the mtime of the directory it appears in,
    which is on that path. Database stores are one file plus their WAL. Empty string
    means "cannot tell".
    """
    location = store.location
    parts: list[str] = []
    try:
        if os.path.isdir(location):
            dirs: set[str] = {location}
            for path in known_files:
                parent = os.path.dirname(path)
                while parent.startswith(location) and parent not in dirs:
                    dirs.add(parent)
                    parent = os.path.dirname(parent)
            for path in sorted(dirs):
                try:
                    parts.append(str(os.stat(path).st_mtime_ns))
                except OSError:
                    parts.append("gone")
            for path in known_files:
                try:
                    st = os.stat(path)
                    parts.append(f"{st.st_size}:{st.st_mtime_ns}")
                except OSError:
                    parts.append("gone")
        else:
            for path in (location, location + "-wal"):
                try:
                    st = os.stat(path)
                    parts.append(f"{st.st_size}:{st.st_mtime_ns}")
                except OSError:
                    parts.append("none")
    except OSError:
        return ""
    import hashlib

    return hashlib.blake2s("|".join(parts).encode(), digest_size=12).hexdigest()
