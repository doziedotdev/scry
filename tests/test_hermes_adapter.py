from pathlib import Path
import json
import shutil
import sqlite3
from types import SimpleNamespace

from scry.adapters.base import StoreSpec
from scry.adapters.hermes import HermesAdapter
from tests.adapter_contract import check_store

FIXTURE = Path(__file__).parent / "fixtures/hermes/state.db"


def test_hermes_store(tmp_path):
    adapter = HermesAdapter()
    store = StoreSpec("hermes.fixture", "hermes", str(FIXTURE))
    report = check_store(adapter, store)
    assert set(report.sessions) == {"root-demo", "second-demo"}
    threads = report.sessions["root-demo"]
    assert set(threads) == {(), ("child-demo",), ("child-demo", "grandchild-demo")}
    child = threads[("child-demo",)]
    assert child.meta.kind == "subagent"
    assert child.meta.label == "Inspect soil"
    assert child.meta.task == "Inspect soil"
    assert child.meta.agent_type == "leaf"
    assert (child.meta.spawn_ordinal, child.meta.result_ordinal) == (2, 8)
    assert child.turns[0].speaker == "system"
    assert threads[("child-demo", "grandchild-demo")].meta.label == "delegate"
    turns = threads[()].turns
    assert turns[0].speaker == "user" and turns[0].ts == 1700000001
    assert turns[1].thinking == "Compare drainage options."
    assert turns[1].text == "I will inspect the soil and notes."
    assert [call.name for call in turns[1].calls] == ["delegate_task", "read_file"]
    assert json.loads(turns[1].calls[0].args) == {"goal": "Inspect soil", "role": "leaf"}
    assert turns[2].tool == "delegate_task" and not turns[2].is_error
    assert turns[3].tool == "read_file" and turns[3].is_error
    assert turns[3].call_id == "call-read"
    assert turns[4].speaker == "system"
    assert turns[5].speaker == "summary"  # carries the _compressed_summary marker
    assert turns[6].speaker == "assistant"  # compacted=1 means folded away later, not a summary
    source = next(s for s in adapter.discover(store) if s.session_id == "root-demo")
    meta = adapter.session_meta(source)
    assert meta.project == "/home/example/garden"
    assert meta.title == "Synthetic garden plan"
    assert meta.status == "ended" and meta.extra["end_reason"] == "complete"
    assert meta.model == "fixture-model"
    assert all(turn.offset is None for turn in turns)
    assert threads[()].checkpoint.offset == 0
    profile = tmp_path / "profiles/new-profile"
    profile.mkdir(parents=True)
    shutil.copyfile(FIXTURE, profile / "state.db")
    banks = adapter.stores(SimpleNamespace(hermes_dir=tmp_path))
    assert [(b.name, b.exists) for b in banks] == [("hermes", False), ("hermes.new-profile", True)]
    assert len(list(adapter.discover(banks[1]))) == 2


def test_hermes_incremental(tmp_path):
    path = tmp_path / "state.db"
    shutil.copyfile(FIXTURE, path)
    adapter = HermesAdapter()
    store = StoreSpec("hermes", "hermes", str(path))
    source = next(s for s in adapter.discover(store) if s.session_id == "root-demo")
    thread = source.thread(())
    full = adapter.read_thread(source, thread, None)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES ('root-demo','user','Add mint.',1700000070)")
    added = adapter.read_thread(source, thread, full.checkpoint)
    assert not added.reset
    assert [(t.ordinal, t.speaker, t.text) for t in added.turns] == [(9, "user", "Add mint.")]
    assert adapter.read_thread(source, thread, added.checkpoint).turns == []
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM messages WHERE session_id='root-demo' AND id>1")
    reset = adapter.read_thread(source, thread, added.checkpoint)
    assert reset.reset
    assert [(t.ordinal, t.text) for t in reset.turns] == [(1, "Plan a synthetic garden.")]
