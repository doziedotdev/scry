"""Sub-turns: skill prompts and bus mail as ``@N.k`` entries that never move integer refs.

The ``omp-subturns`` fixture store holds one session whose file interleaves
ordinary messages with ``skill-prompt`` and ``bus:mail`` custom messages
(including a skill prompt before the first message, two skills in a row, two
mails in one line, and skill rows with missing ``args``), plus ``bus:card``
and ``irc:incoming`` rows that must stay unindexed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scry.adapters.base import StoreSpec
from scry.adapters.omp import OmpAdapter, _sub_turn
from scry.config import Config
from scry.index import Index
from scry.model import RefError, parse_ref
from scry.query import find
from scry.views import Renderer
from tests.adapter_contract import check_store

FIXTURES = Path(__file__).parent / "fixtures" / "omp-subturns"
REF_RE = re.compile(r"ref=([^\s()]+)")

EXPECTED = [
    ("0.1", "user", "[skill retro] evening review pass"),
    ("1", "user", "Let's start the review."),
    ("2", "assistant", "Starting now."),
    ("2.1", "user", "[skill deep-design-meditation] think harder about the steer"),
    ("2.2", "user", "[skill grilling] grill the plan before building"),
    ("2.3", "mail", "from quoter (report, hold, omp:fixsub01-peer): status one bramble\n"
                    "from quoter (peer, wake, omp:fixsub01-peer): status two bramble"),
    ("3", "user", "What did the review find?"),
    ("4", "assistant", "Two findings so far."),
    ("4.1", "user", "[skill retro] wrap up with fallback args"),
    ("4.2", "user", "[skill retro] "),
]


def _source():
    adapter = OmpAdapter()
    store = StoreSpec("omp", "omp", str(FIXTURES))
    source = next(s for s in adapter.discover(store) if s.session_id == "fixsub01")
    return adapter, source


# ------------------------------------------------------------------ adapter


def test_skill_prompts_and_bus_mail_become_sub_turns():
    adapter, source = _source()
    read = adapter.read_thread(source, source.thread(()), None)
    assert [(t.label, t.speaker, t.text) for t in read.turns] == EXPECTED
    assert (read.checkpoint.ordinal, read.checkpoint.sub) == (4, 2)


def test_hidden_and_unrelated_custom_messages_stay_unindexed():
    adapter, source = _source()
    read = adapter.read_thread(source, source.thread(()), None)
    blob = "\n".join(t.text for t in read.turns)
    assert "held mail summary" not in blob  # bus:card, display false
    assert "hello from irc" not in blob  # irc:incoming
    assert "Full skill below" not in blob  # a skill body is never indexed


def test_sub_turn_unit_edges():
    base = {"type": "custom_message", "display": True, "id": "x"}
    skill = dict(base, customType="skill-prompt", attribution="agent",
                 details={"name": "grilling", "args": "go"}, content="body")
    assert (_sub_turn(skill, 1, 1, 0).speaker, _sub_turn(skill, 1, 1, 0).text) == ("system", "[skill grilling] go")
    nameless = dict(base, customType="skill-prompt", attribution="user", details={}, content="body")
    assert _sub_turn(nameless, 0, 1, 0).text == "[skill ?] "
    mail = dict(base, customType="bus:mail", details={}, content="<bus-mail>raw card</bus-mail>")
    assert _sub_turn(mail, 1, 1, 0).text == "<bus-mail>raw card</bus-mail>"
    assert _sub_turn(dict(base, customType="bus:card", details={}), 1, 1, 0) is None
    assert _sub_turn(dict(base, customType="bus:mail", display=False, details={}), 1, 1, 0) is None
    assert _sub_turn({"type": "message"}, 1, 1, 0) is None


def test_contract_holds_with_sub_turns_present():
    report = check_store(OmpAdapter(), StoreSpec("omp", "omp", str(FIXTURES)))
    assert set(report.sessions) == {"fixsub01"}


def test_integer_ordinals_unchanged_by_sub_turns(tmp_path):
    """The same file with its sub-turn rows stripped yields identical integer turns."""
    adapter = OmpAdapter()
    store = StoreSpec("omp", "omp", str(FIXTURES))
    source = next(s for s in adapter.discover(store) if s.session_id == "fixsub01")
    full = adapter.read_thread(source, source.thread(()), None)
    integers = [(t.ordinal, t.speaker, t.text, t.native_id) for t in full.turns if t.sub == 0]

    copied = tmp_path / "omp"
    (copied / "-work-sub").mkdir(parents=True)
    src = Path(source.thread(()).location)
    rows = [json.loads(line) for line in src.read_text().splitlines()]
    kept = [r for r in rows if not (r.get("type") == "custom_message" and r.get("customType") in ("skill-prompt", "bus:mail"))]
    assert len(kept) < len(rows)
    (copied / "-work-sub" / src.name).write_text("\n".join(json.dumps(r) for r in kept) + "\n")
    stripped_source = next(s for s in adapter.discover(StoreSpec("omp", "omp", str(copied))) if s.session_id == "fixsub01")
    stripped = adapter.read_thread(stripped_source, stripped_source.thread(()), None)
    assert [(t.ordinal, t.speaker, t.text, t.native_id) for t in stripped.turns] == integers


def test_incremental_read_continues_sub_numbering(tmp_path):
    import shutil

    copied = tmp_path / "omp"
    shutil.copytree(FIXTURES, copied)
    adapter = OmpAdapter()
    store = StoreSpec("omp", "omp", str(copied))
    source = next(s for s in adapter.discover(store) if s.session_id == "fixsub01")
    thread = source.thread(())
    full = adapter.read_thread(source, thread, None)
    assert (full.checkpoint.ordinal, full.checkpoint.sub) == (4, 2)

    path = Path(thread.location)
    skill = {"type": "custom_message", "customType": "skill-prompt", "display": True, "attribution": "user",
             "id": "sk5", "timestamp": "2026-09-10T10:09:00Z", "content": "body\nUser: one more angle",
             "details": {"name": "retro", "path": "/skills/retro/SKILL.md", "args": "one more angle", "lineCount": 4}}
    with path.open("a") as handle:
        handle.write(json.dumps(skill) + "\n")
    resumed = adapter.read_thread(source, thread, full.checkpoint)
    assert not resumed.reset
    assert [(t.label, t.text) for t in resumed.turns] == [("4.3", "[skill retro] one more angle")]
    assert (resumed.checkpoint.ordinal, resumed.checkpoint.sub) == (4, 3)

    user = {"type": "message", "id": "u3", "timestamp": "2026-09-10T10:10:00Z",
            "message": {"role": "user", "content": "Thanks, done."}}
    with path.open("a") as handle:
        handle.write(json.dumps(user) + "\n" + json.dumps(skill | {"id": "sk6"}) + "\n")
    again = adapter.read_thread(source, thread, resumed.checkpoint)
    assert [(t.label, t.speaker) for t in again.turns] == [("5", "user"), ("5.1", "user")]
    assert (again.checkpoint.ordinal, again.checkpoint.sub) == (5, 1)


# --------------------------------------------------------------------- refs


@pytest.mark.parametrize("text, start, sub", [("omp:fixsub01@2.1", 2, 1), ("omp:fixsub01@0.1", 0, 1), ("fixsub01@2480.12", 2480, 12)])
def test_parse_and_print_sub_turn_refs(text, start, sub):
    ref = parse_ref(text)
    assert (ref.turns.start, ref.turns.end, ref.turns.sub) == (start, start, sub)
    assert str(ref) == text


@pytest.mark.parametrize("text", ["omp:fixsub01@2.0", "omp:fixsub01@0", "omp:fixsub01@2.1-3", "omp:fixsub01@2-3.1", "omp:fixsub01@2.", "omp:fixsub01@.2"])
def test_rejects_malformed_sub_turn_refs(text):
    with pytest.raises(RefError):
        parse_ref(text)


def test_single_means_the_integer_turn_only():
    assert parse_ref("omp:fixsub01@2").turns.single
    assert parse_ref("omp:fixsub01@2-2").turns.single
    assert not parse_ref("omp:fixsub01@2-3").turns.single
    assert not parse_ref("omp:fixsub01@2-").turns.single
    assert not parse_ref("omp:fixsub01@-3").turns.single
    assert not parse_ref("omp:fixsub01@2.1").turns.single


# ------------------------------------------------------------- index/views


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("subturns")
    return Config(
        home=tmp, config_path=tmp / "config.toml", index_path=tmp / "index.sqlite3", export_dir=tmp / "exports",
        claude_projects_dir=tmp / "no-claude", cursor_projects_dir=tmp / "no-cursor", cursor_state_db=tmp / "no-state",
        omp_sessions_dir=FIXTURES, hermes_dir=tmp / "no-hermes",
    )


@pytest.fixture(scope="module")
def index(cfg):
    idx = Index(cfg)
    report = idx.run()
    assert not report.errors, report.errors
    yield idx
    idx.close()


@pytest.fixture(scope="module")
def renderer(index, cfg):
    return Renderer(index, cfg)


def test_turn_count_counts_integer_turns(index):
    assert index.resolve(parse_ref("omp:fixsub01")).turn_count == 4


def test_resolve_sub_turn(index):
    resolved = index.resolve(parse_ref("omp:fixsub01@2.1"))
    assert (resolved.turns, resolved.sub) == ((2, 2), 1)
    zero = index.resolve(parse_ref("omp:fixsub01@0.1"))
    assert (zero.turns, zero.sub) == ((0, 0), 1)
    with pytest.raises(RefError, match="sub-turns 1-3"):
        index.resolve(parse_ref("omp:fixsub01@2.9"))
    with pytest.raises(RefError, match="has no sub-turns"):
        index.resolve(parse_ref("omp:fixsub01@3.1"))


def test_read_single_integer_turn_excludes_its_sub_turns(index, renderer):
    text = renderer.read(index.resolve(parse_ref("omp:fixsub01@2")), 4000, full=False).text
    assert "Starting now." in text
    assert "think harder" not in text and "@2.1" not in text


def test_read_range_includes_the_sub_turns_inside(index, renderer):
    text = renderer.read(index.resolve(parse_ref("omp:fixsub01@2-3")), 4000, full=True).text
    assert "@2.1" in text and "@2.2" in text and "@2.3" in text
    assert "think harder about the steer" in text and "status two bramble" in text


def test_read_one_sub_turn(index, renderer):
    view = renderer.read(index.resolve(parse_ref("omp:fixsub01@2.3")), 4000, full=True)
    assert "read omp:fixsub01@2.3" in view.text
    assert "from quoter (report, hold, omp:fixsub01-peer): status one bramble" in view.text
    assert view.refs[0].endswith("@2.3")


def test_spans_from_the_start_include_leading_sub_turns(index, renderer):
    text = renderer.read(index.resolve(parse_ref("omp:fixsub01@1-2")), 4000, full=True).text
    assert "@0.1" in text and "evening review pass" in text


def test_walk_shows_sub_turns_and_never_folds_mail(index, renderer):
    text = renderer.walk(index.resolve(parse_ref("omp:fixsub01")), 4000, speakers=None, start=None, limit=60).text
    assert "@0.1" in text and "@2.3" in text and "@4.2" in text
    assert "mail" in text and "bramble" in text
    assert "[1 tool turns" not in text  # no fold ran; and mail lines are never folded


def test_walk_single_integer_turn(index, renderer):
    text = renderer.walk(index.resolve(parse_ref("omp:fixsub01@2")), 4000, speakers=None, start=None, limit=60).text
    assert "@2 " in text or "@2\n" in text
    assert "2.1" not in text


def test_find_mail_with_default_speakers(index, renderer):
    result = find(index, "bramble")
    assert result.groups and result.groups[0].hits[0].speaker == "mail"
    assert str(result.groups[0].hits[0].ref) == "omp:fixsub01@2.3"
    text = renderer.find(result, 4000, q="bramble", scope_note="", speakers=("user", "assistant", "summary", "mail")).text
    assert "@2.3" in text


def test_find_skill_words_as_the_user(index):
    result = find(index, "grill plan", speakers=("user",))
    assert [str(h.ref) for g in result.groups for h in g.hits] == ["omp:fixsub01@2.2"]


def test_card_counts_sub_turns_and_labels_first_user(index, renderer):
    text = renderer.card(index.resolve(parse_ref("omp:fixsub01")), 4000).text
    assert "turns 4 +6 sub-turns" in text
    assert "mail 1" in text
    assert "first user @0.1" in text


@pytest.mark.parametrize("budget", [700, 4000])
def test_every_printed_ref_resolves(index, renderer, budget):
    session = index.resolve(parse_ref("omp:fixsub01"))
    result = find(index, "bramble")
    texts = [
        renderer.find(result, budget, q="bramble", scope_note="", speakers=("user", "assistant", "summary", "mail")).text,
        renderer.walk(session, budget, speakers=None, start=None, limit=60).text,
        renderer.read(index.resolve(parse_ref("omp:fixsub01@2-3")), budget, full=False).text,
        renderer.read(index.resolve(parse_ref("omp:fixsub01@2.3")), budget, full=False).text,
        renderer.card(session, budget).text,
    ]
    for text in texts:
        assert len(text) <= budget + 1
        for token in REF_RE.findall(text):
            token = token.rstrip(".,;:")
            if "<" in token:  # templates like ref=<session>
                continue
            try:
                index.resolve(parse_ref(token))
            except RefError as exc:
                raise AssertionError(f"unresolvable {token}: {exc}") from exc


def test_index_append_continues_sub_turns(tmp_path):
    import shutil

    copied = tmp_path / "omp-live"
    shutil.copytree(FIXTURES, copied)
    cfg = Config(
        home=tmp_path, config_path=tmp_path / "config.toml", index_path=tmp_path / "index.sqlite3",
        export_dir=tmp_path / "exports", claude_projects_dir=tmp_path / "no-claude",
        cursor_projects_dir=tmp_path / "no-cursor", cursor_state_db=tmp_path / "no-state",
        omp_sessions_dir=copied, hermes_dir=tmp_path / "no-hermes",
    )
    idx = Index(cfg)
    try:
        assert not idx.run().errors
        assert idx.resolve(parse_ref("omp:fixsub01")).turn_count == 4
        path = next((copied / "-work-sub").glob("*.jsonl"))
        skill = {"type": "custom_message", "customType": "skill-prompt", "display": True, "attribution": "user",
                 "id": "sk-live", "timestamp": "2026-09-10T10:09:00Z", "content": "body\nUser: live angle",
                 "details": {"name": "retro", "path": "/skills/retro/SKILL.md", "args": "live angle", "lineCount": 4}}
        with path.open("a") as handle:
            handle.write(json.dumps(skill) + "\n")
        report = idx.run()
        assert not report.errors and report.turns_added == 1
        resolved = idx.resolve(parse_ref("omp:fixsub01@4.3"))
        assert resolved.turn_count == 4 and resolved.sub == 3
        row = idx.turn(resolved.thread_pk, 4, 3)
        assert row["text"] == "[skill retro] live angle"
    finally:
        idx.close()
