import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scry.adapters.base import StoreSpec
from scry.adapters.cursor import CursorAdapter, parse_cursor_timestamp
from tests.adapter_contract import check_store

FIXTURE = Path(__file__).parent / "fixtures/cursor"


def test_cursor_store():
    adapter = CursorAdapter()
    store = adapter.stores(SimpleNamespace(cursor_projects_dir=FIXTURE / "projects",
                                          cursor_state_db=FIXTURE / "state.vscdb"))[0]
    report = check_store(adapter, store)
    assert {"composer-main", "composer-short"} <= set(report.sessions)
    threads = report.sessions["composer-main"]
    assert set(threads) == {(), ("child-scout",)}
    main = threads[()]
    child = threads[("child-scout",)]
    assert child.meta.kind == "subagent"
    assert child.meta.label == "SampleScout"
    assert child.meta.spawn_ordinal == 2
    assert child.meta.result_ordinal is None
    assert child.meta.task == "Inspect the sample"
    assert child.meta.agent_type == "scout"
    assert [turn.speaker for turn in main.turns] == ["user", "assistant", "system", "assistant"]
    assert main.turns[0].text == "Inspect & explain the sample."
    timestamp = datetime(2026, 9, 2, 18, 43, tzinfo=timezone.utc).timestamp()
    assert main.turns[0].ts == timestamp
    assert main.turns[1].ts == timestamp
    assert main.turns[1].thinking == "Inspect the synthetic sample carefully."
    assert [call.name for call in main.turns[1].calls] == ["Task", "Read"]
    assert main.turns[1].calls[1].call_id == "read-1"
    assert json.loads(main.turns[1].calls[1].args) == {"path": "/synthetic/sample.txt"}
    # No tool-result messages exist in Cursor; error lifecycle markers add no turn.
    assert not any(turn.speaker == "tool" or turn.is_error for turn in main.turns)
    source = next(item for item in adapter.discover(store) if item.session_id == "composer-main")
    meta = adapter.session_meta(source)
    assert meta.title == "Synthetic sample review"
    assert meta.project == "/home/synthetic/work"
    assert meta.project_slug == "home-synthetic-work"
    assert meta.status == "completed"
    assert meta.started == timestamp
    for wrapper in ("Sep 2, 2026, 2:43 PM (UTC-4)",
                    "Wednesday, September 2, 2026, 2:43 PM (UTC-4)",
                    "September 2, 2026, 6:43 PM"):
        assert parse_cursor_timestamp(f"<timestamp>{wrapper}</timestamp>") == timestamp
    assert parse_cursor_timestamp("<timestamp>Sep 2, 2026, 2:43 PM (UTC+99)</timestamp>") is None
    no_db = CursorAdapter(state_db=FIXTURE / "missing.vscdb")
    assert no_db.session_meta(source).title == ""
    assert no_db.read_thread(source, source.threads[0], None).turns == main.turns
    # A mirror does not borrow names from this machine's configured database.
    mirror = StoreSpec("cursor.mirror", "cursor", str(FIXTURE / "projects"))
    assert check_store(CursorAdapter(), mirror).sessions["composer-main"][("child-scout",)].meta.label == "sub-child-sc"


def test_cursor_incremental(tmp_path):
    shutil.copytree(FIXTURE / "projects", tmp_path / "projects")
    adapter = CursorAdapter()
    store = StoreSpec("cursor", "cursor", str(tmp_path / "projects"))
    source = next(item for item in adapter.discover(store) if item.session_id == "composer-main")
    thread = source.threads[0]
    first = adapter.read_thread(source, thread, None)
    path = Path(thread.location)
    row = {"role": "assistant", "message": {"content": [{"type": "text", "text": "One appended message."}]}}
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    appended = adapter.read_thread(source, thread, first.checkpoint)
    assert [(turn.ordinal, turn.text, turn.ts) for turn in appended.turns] == [
        (5, "One appended message.", first.turns[-1].ts)]
    assert not appended.reset
    assert not adapter.read_thread(source, thread, appended.checkpoint).turns
    # Replacing content without shrinking must not silently retain stale turns.
    data = path.read_text()
    path.write_text(data.replace("Inspection complete.", "Inspection altered!."))
    replaced = adapter.read_thread(source, thread, appended.checkpoint)
    assert replaced.reset
    assert replaced.turns[3].text == "Inspection altered!."
    path.write_text(json.dumps(row) + "\n")
    truncated = adapter.read_thread(source, thread, replaced.checkpoint)
    assert truncated.reset
    assert [(turn.ordinal, turn.text) for turn in truncated.turns] == [(1, "One appended message.")]
