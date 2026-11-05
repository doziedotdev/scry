import json
from pathlib import Path
import shutil

from scry.adapters.base import StoreSpec, parse_timestamp
from scry.adapters.omp import OmpAdapter
from tests.adapter_contract import check_store

FIXTURES = Path(__file__).parent / "fixtures" / "omp"


def test_omp_store():
    adapter = OmpAdapter()
    store = StoreSpec("omp.mirror", "omp", str(FIXTURES))
    report = check_store(adapter, store)
    assert set(report.sessions) == {"fixture001", "fixture002"}
    tree = report.sessions["fixture001"]
    assert {path: (read.meta.kind, read.meta.label) for path, read in tree.items()} == {
        (): ("main", "main"), ("Scout",): ("subagent", "Scout"),
        ("__advisor.reviewer",): ("advisor", "__advisor.reviewer"),
    }
    main = tree[()].turns
    assert [t.speaker for t in main] == ["user", "assistant", "tool", "tool", "system", "summary", "system"]
    assert main[1].text == "Starting now."
    assert main[1].thinking == "I will inspect two sources."
    assert [c.name for c in main[1].calls] == ["task", "read"]
    assert main[1].calls[0].intent == "Delegating inspection"
    assert json.loads(main[1].calls[0].args)["tasks"][0]["name"] == "Scout"
    assert (main[2].tool, main[2].is_error, main[2].call_id) == ("read", True, "read1")
    assert (main[3].tool, main[3].is_error) == ("task", False)
    assert main[0].ts == parse_timestamp("2026-09-01T10:01:00Z")
    assert main[5].text == "Sample inspection summarized."
    scout = tree[("Scout",)].meta
    assert (scout.agent_type, scout.task, scout.model) == ("scout", "Inspect sample", "synthetic/fast")
    assert (scout.spawn_ordinal, scout.result_ordinal) == (2, 4)
    advisor = tree[("__advisor.reviewer",)].meta
    assert advisor.agent_type == "reviewer"
    assert advisor.spawn_ordinal is None and advisor.result_ordinal is None
    source = next(s for s in adapter.discover(store) if s.session_id == "fixture001")
    meta = adapter.session_meta(source)
    assert (meta.project, meta.title) == ("/work/synthetic", "Synthetic OMP tree")
    assert meta.native_handle == "history://fixture001"
    assert {(a.name, a.kind, a.owner) for a in meta.artifacts} == {
        ("Scout.md", "output", "Scout"), ("Scout.json", "output", "Scout"),
        ("001.read.log", "log", ""),
    }


def test_omp_incremental(tmp_path):
    copied = tmp_path / "omp"
    shutil.copytree(FIXTURES, copied)
    adapter = OmpAdapter()
    source = next(s for s in adapter.discover(StoreSpec("omp", "omp", str(copied))) if s.session_id == "fixture001")
    thread = source.thread(())
    full = adapter.read_thread(source, thread, None)
    path = Path(thread.location)
    row = {"type": "message", "id": "appended", "message": {"role": "user", "content": "One more question."}}
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    resumed = adapter.read_thread(source, thread, full.checkpoint)
    assert not resumed.reset
    assert [(t.ordinal, t.text) for t in resumed.turns] == [(full.checkpoint.ordinal + 1, "One more question.")]
    assert not adapter.read_thread(source, thread, resumed.checkpoint).turns
    path.write_text(json.dumps(row) + "\n")
    reset = adapter.read_thread(source, thread, resumed.checkpoint)
    assert reset.reset
    assert [(t.ordinal, t.text) for t in reset.turns] == [(1, "One more question.")]


def test_omp_ask_answers_are_user_turns(tmp_path):
    copied = tmp_path / "omp"
    shutil.copytree(FIXTURES, copied)
    path = copied / "-work-synthetic" / "2026-09-02T10-00-00-000Z_fixture002.jsonl"

    def result(call_id, content, details=None, error=False):
        message = {"role": "toolResult", "toolCallId": call_id, "toolName": "ask", "isError": error,
                   "content": [{"type": "text", "text": content}], "timestamp": 1788000000000}
        if details is not None:
            message["details"] = details
        return {"type": "message", "id": call_id + "-r", "message": message}

    rows = [
        result("one", "[shaken ~40 tokens]", {"question": "Repair now?", "options": ["Yes", "Hold"],
                                               "multi": False, "selectedOptions": ["Hold"]}),
        result("many", "[shaken ~79 tokens]", {"results": [
            {"id": "a", "question": "Which levels?", "options": ["None", "Employee"], "multi": True,
             "selectedOptions": ["None", "Employee"], "customInput": "and Partner"},
            {"id": "b", "question": "Which login?", "options": ["Service"], "multi": False,
             "selectedOptions": [], "customInput": "my own creds for now"}]}),
        result("cut", "Ask interrupted by message", {}, error=True),
        {"type": "message", "id": "read-r", "message": {"role": "toolResult", "toolCallId": "read", "toolName": "read",
                                                        "content": [{"type": "text", "text": "file text"}]}},
    ]
    with path.open("a") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    adapter = OmpAdapter()
    source = next(s for s in adapter.discover(StoreSpec("omp", "omp", str(copied))) if s.session_id == "fixture002")
    turns = adapter.read_thread(source, source.thread(()), None).turns[-4:]
    assert [(t.speaker, t.tool, t.call_id, t.is_error) for t in turns] == [
        ("user", "ask", "one", False), ("user", "ask", "many", False),
        ("tool", "ask", "cut", True), ("tool", "read", "read", False)]
    assert turns[0].text == "Hold  ← Repair now?"
    assert turns[1].text.splitlines() == ["None, Employee; and Partner  ← Which levels?",
                                          "my own creds for now  ← Which login?"]
    assert turns[2].text == "Ask interrupted by message"
