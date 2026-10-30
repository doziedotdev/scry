"""What a reader observes from every view over the fixture stores.

Two invariants hold for every view at every budget: the text never exceeds the
budget, and every ``ref=`` it prints resolves. Then the behaviors a consumer relies
on: find tiers, session grouping, user-only walks, read caps that point onward,
export to a file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scry.config import Config
from scry.index import Index
from scry.model import RefError, parse_ref
from scry.query import Scope, find
from scry.views import Renderer

FIXTURES = Path(__file__).parent / "fixtures"
REF_RE = re.compile(r"ref=([^\s()]+)")


@pytest.fixture(scope="module")
def cfg(tmp_path_factory) -> Config:
    tmp = tmp_path_factory.mktemp("ts")
    return Config(
        home=tmp, config_path=tmp / "config.toml", index_path=tmp / "index.sqlite3", export_dir=tmp / "exports",
        claude_projects_dir=FIXTURES / "claude", cursor_projects_dir=FIXTURES / "cursor" / "projects",
        cursor_state_db=FIXTURES / "cursor" / "state.vscdb", omp_sessions_dir=FIXTURES / "omp", hermes_dir=FIXTURES / "hermes",
    )


@pytest.fixture(scope="module")
def index(cfg) -> Index:
    idx = Index(cfg)
    report = idx.run()
    assert not report.errors, report.errors
    assert {"omp", "claude", "cursor", "hermes"} <= set(report.stores)
    yield idx
    idx.close()


@pytest.fixture(scope="module")
def renderer(index, cfg) -> Renderer:
    return Renderer(index, cfg)


def _every_view(index: Index, renderer: Renderer, budget: int) -> list[tuple[str, str]]:
    views = [("overview", renderer.overview(budget).text), ("projects", renderer.projects(index.projects(), budget).text)]
    rows = index.sessions(stores=None, project=None, since=None, until=None, limit=50)
    views.append(("sessions", renderer.sessions(rows, budget, title="sessions").text))
    views.append(("find", renderer.find(find(index, "inspect sample", limit=5), budget, q="inspect sample", scope_note="", speakers=("user", "assistant", "summary")).text))
    for row in rows:
        ref = f"{row['store']}:{row['session_id']}"
        resolved = index.resolve(parse_ref(ref))
        views.append((f"card {ref}", renderer.card(resolved, budget).text))
        views.append((f"tree {ref}", renderer.tree(resolved, budget).text))
        views.append((f"links {ref}", renderer.links(resolved, budget).text))
        views.append((f"walk {ref}", renderer.walk(resolved, budget, speakers=None, start=None, limit=60).text))
        first_turns = index.resolve(parse_ref(f"{ref}@1-3"))
        views.append((f"read {ref}", renderer.read(first_turns, budget, full=False).text))
        for thread in index.thread_rows(resolved.session_pk):
            if thread["path"]:
                child = index.resolve(parse_ref(f"{ref}/{thread['path']}"))
                views.append((f"card {ref}/{thread['path']}", renderer.card(child, budget).text))
    return views


@pytest.mark.parametrize("budget", [200, 300, 700, 1500, 4000])
def test_every_view_fits_its_budget_and_every_ref_resolves(index, renderer, budget):
    for name, text in _every_view(index, renderer, budget):
        assert len(text) <= budget, f"{name} at budget {budget}: {len(text)} chars"
        assert text.strip(), f"{name} rendered nothing"
        head = text.splitlines()[0]
        assert head.startswith(("transcripts:", "projects:", "sessions", "find ", "tree ", "walk ", "read ", "links ", "omp:", "claude:", "cursor:", "hermes", "agy:")), f"{name} at budget {budget} lost its header: {head!r}"
        for token in REF_RE.findall(text):
            if "<" in token:  # templates like ref=<session>
                continue
            try:
                index.resolve(parse_ref(token))
            except RefError as exc:
                raise AssertionError(f"{name} at budget {budget} printed unresolvable {token}: {exc}") from exc


def test_find_groups_hits_by_session_and_names_the_tier(index, renderer):
    result = find(index, "inspect sample")
    assert result.tier == "turn"
    assert result.groups and all(g.total >= 1 for g in result.groups)
    text = renderer.find(result, 2000, q="inspect sample", scope_note="", speakers=("user", "assistant", "summary")).text
    assert "match: all terms in one turn" in text
    assert "@" in text and "next: ref=" in text


def test_find_falls_back_to_session_scope_then_reports_per_term_counts(index):
    # "delegated" is in the child agent, "compaction"-era words in the main thread: same session, never one turn.
    result = find(index, "delegated summarized", speakers=("user", "assistant", "summary"))
    assert result.tier in ("session", "any")
    assert result.per_term and set(result.per_term) == {"delegated", "summarized"}
    nothing = find(index, "zzqx-not-a-word")
    assert nothing.groups == [] and nothing.per_term == {"zzqx-not-a-word": 0}


def test_find_can_be_scoped_to_one_session(index):
    session = index.resolve(parse_ref("omp:fixture001"))
    inside = find(index, "inspect", scope=Scope(session_pk=session.session_pk))
    assert inside.groups and all(g.session_pk == session.session_pk for g in inside.groups)


def test_walk_with_speaker_user_tells_only_the_asks(index, renderer):
    resolved = index.resolve(parse_ref("omp:fixture001"))
    text = renderer.walk(resolved, 4000, speakers=("user",), start=None, limit=60).text
    body = [line for line in text.splitlines()[1:] if line.startswith("@")]
    assert body and all(" user " in line for line in body)
    assert "Injected notification" not in text  # a synthetic user row is system, not a human ask


def test_read_caps_each_turn_and_points_at_the_rest(index, renderer):
    resolved = index.resolve(parse_ref("claude:session-001@1-2"))
    text = renderer.read(resolved, 300, full=False).text
    assert len(text) <= 300
    assert "ref=claude:session-001@" in text  # the cut or the next pointer names a turn


def test_read_of_one_turn_at_a_tight_budget_shows_its_head_not_only_a_pointer(index, renderer):
    # A turn larger than the per-turn share used to be dropped whole, leaving only
    # "more: ... (or raise budget)". The reader must see the turn's head instead.
    for ref in ("omp:fixture001@2", "claude:session-001@2"):
        resolved = index.resolve(parse_ref(ref))
        text = renderer.read(resolved, 480, full=False).text
        assert len(text) <= 480
        lines = text.splitlines()
        assert lines[1].startswith(f"── {ref}"), text
        assert "more: ref=" not in text, text


def test_card_shows_how_the_session_ended(index, renderer):
    resolved = index.resolve(parse_ref("omp:fixture001"))
    text = renderer.card(resolved, 4000).text
    assert "last assistant @" in text
    assert "/Scout" in text and "advisor" in text
    assert "summary @" in text


def test_prefix_and_bare_ids_resolve_and_ambiguity_is_explained(index):
    assert index.resolve(parse_ref("fixture001")).ref.store == "omp"
    with pytest.raises(RefError) as err:
        index.resolve(parse_ref("omp:fixture"))
    assert "matches 2 sessions" in str(err.value)
    with pytest.raises(RefError) as err:
        index.resolve(parse_ref("omp:fixture001/Nope"))
    assert "agents here" in str(err.value)


def test_export_writes_a_file_and_returns_its_pointer(index, renderer, cfg):
    resolved = index.resolve(parse_ref("omp:fixture001"))
    view = renderer.export(resolved, None)
    path = Path(view.meta["path"])
    assert path.is_file() and path.stat().st_size == view.meta["bytes"] > 0
    assert str(path) in view.text and str(path).startswith(str(cfg.export_dir))


def test_second_index_pass_reads_nothing(index):
    report = index.run()
    assert report.turns_added == 0 and report.threads_updated == 0 and not report.errors
