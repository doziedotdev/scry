"""The catch-up pass must be cheap when nothing changed and must never miss a change.

What it defends: the store signature gate skips discovery only when it is safe. A new
session, a new agent file, an appended turn on a live session, and a truncated file
must each be picked up by the next pass; an unchanged tree must read nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scry.config import Config
from scry.index import Index
from scry.model import RefError, StoreSpec, parse_ref

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workspace(tmp_path):
    omp = tmp_path / "omp"
    shutil.copytree(FIXTURES / "omp", omp)
    cfg = Config(
        home=tmp_path, config_path=tmp_path / "c.toml", index_path=tmp_path / "i.sqlite3", export_dir=tmp_path / "e",
        claude_projects_dir=tmp_path / "none-claude", cursor_projects_dir=tmp_path / "none-cursor",
        cursor_state_db=tmp_path / "none.vscdb", omp_sessions_dir=omp, hermes_dir=tmp_path / "none-hermes",
    )
    idx = Index(cfg)
    first = idx.run()
    assert first.turns_added > 0 and not first.errors
    yield idx, omp
    idx.close()


def _bump(path: Path) -> None:
    """Make an mtime change observable even on coarse filesystems."""
    now = time.time() + 2
    os.utime(path, (now, now))


def test_unchanged_store_reads_nothing(workspace):
    idx, _ = workspace
    report = idx.run()
    assert report.threads_updated == 0 and report.turns_added == 0


def test_store_removed_from_config_is_dropped(workspace, tmp_path):
    idx, omp = workspace
    mirror = tmp_path / "mirror"
    shutil.copytree(omp, mirror)
    with_mirror = replace(idx.cfg, extra_stores=(StoreSpec("omp.mirror", "omp", str(mirror), True),))
    first = Index(with_mirror)
    try:
        assert "omp.mirror" in first.run().stores
        assert first.resolve(parse_ref("omp.mirror:fixture001")).turn_count > 0
    finally:
        first.close()
    report = idx.run()
    assert report.sessions_removed > 0
    assert "omp.mirror" not in {row["name"] for row in idx.stores()}
    with pytest.raises(RefError):
        idx.resolve(parse_ref("omp.mirror:fixture001"))
    assert idx.resolve(parse_ref("omp:fixture001")).turn_count > 0


def test_appended_turn_on_a_live_session_is_seen(workspace):
    idx, omp = workspace
    main = next(omp.glob("*/*fixture001.jsonl"))
    before = idx.resolve(parse_ref("omp:fixture001")).turn_count
    with open(main, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "id": "zz1", "parentId": None, "timestamp": "2026-09-01T12:00:00.000Z",
                                 "message": {"role": "user", "content": [{"type": "text", "text": "One more ask."}]}}) + "\n")
    _bump(main)
    report = idx.run()
    assert report.turns_added == 1
    assert idx.resolve(parse_ref("omp:fixture001")).turn_count == before + 1


def test_new_agent_file_is_seen(workspace):
    idx, omp = workspace
    session_dir = next(p for p in omp.glob("*/*") if p.is_dir())
    new_agent = session_dir / "Newcomer.jsonl"
    lines = [
        {"type": "session", "version": 3, "id": "newcomer-1", "timestamp": "2026-09-01T12:00:00.000Z", "cwd": "/work/synthetic"},
        {"type": "message", "id": "n1", "parentId": None, "timestamp": "2026-09-01T12:00:01.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Newcomer reporting."}]}},
    ]
    new_agent.write_text("".join(json.dumps(l) + "\n" for l in lines))
    _bump(session_dir)
    report = idx.run()
    assert report.threads_updated >= 1
    assert idx.resolve(parse_ref("omp:fixture001/Newcomer")).turn_count == 1


def test_new_session_is_seen(workspace):
    idx, omp = workspace
    project = next(p for p in omp.iterdir() if p.is_dir())
    new = project / "2026-09-03T10-00-00-000Z_fixture003.jsonl"
    lines = [
        {"type": "session", "version": 3, "id": "fixture003", "timestamp": "2026-09-03T10:00:00.000Z", "cwd": "/work/synthetic"},
        {"type": "message", "id": "s1", "parentId": None, "timestamp": "2026-09-03T10:00:01.000Z", "message": {"role": "user", "content": [{"type": "text", "text": "Fresh session."}]}},
    ]
    new.write_text("".join(json.dumps(l) + "\n" for l in lines))
    _bump(project)
    idx.run()
    assert idx.resolve(parse_ref("omp:fixture003")).turn_count == 1


def test_truncated_session_resets(workspace):
    idx, omp = workspace
    main = next(omp.glob("*/*fixture001.jsonl"))
    lines = main.read_text().splitlines(keepends=True)
    main.write_text("".join(lines[:3]))
    _bump(main)
    idx.run()
    assert idx.resolve(parse_ref("omp:fixture001")).turn_count < 5
