import json
import shutil
from pathlib import Path

from scry.adapters.base import StoreSpec
from scry.adapters.claude import ClaudeAdapter
from tests.adapter_contract import check_store

FIXTURE = Path(__file__).parent / "fixtures" / "claude"


def test_claude_store():
    adapter = ClaudeAdapter()
    store = StoreSpec("claude.mirror", "claude", str(FIXTURE))
    report = check_store(adapter, store)
    assert set(report.sessions) == {"session-001", "session-002"}
    threads = report.sessions["session-001"]
    assert set(threads) == {(), ("sidechain-side0001",), ("agent-fixture",)}
    main = threads[()]
    assistant = main.turns[1]
    assert assistant.text == "I will inspect the parser."
    assert assistant.thinking == "Check both branches."
    assert [call.name for call in assistant.calls] == ["Read", "Task"]
    assert main.turns[2].speaker == "tool"
    assert main.turns[2].tool == "Read" and main.turns[2].is_error
    assert main.turns[3].tool == "Task" and not main.turns[3].is_error
    assert main.turns[4].speaker == "user" and main.turns[4].text == "Please continue."
    assert main.turns[2].offset == main.turns[3].offset == main.turns[4].offset
    assert main.turns[5].speaker == "system"
    assert main.turns[6].speaker == "summary"
    assert main.turns[0].ts == 1785585600.0
    side = threads[("sidechain-side0001",)]
    assert side.meta.kind == "sidechain" and side.meta.label == "sidechain-side0001"
    assert [turn.text for turn in side.turns] == [
        "Inspect the side branch.\nReport findings.", "Inspecting side branch.", "Side branch complete."]
    assert side.turns[0].speaker == "system"
    assert side.meta.spawn_ordinal == 2 and side.meta.result_ordinal == 4
    assert side.meta.agent_type == "Explore" and side.meta.task == "Inspect the side branch."
    child = threads[("agent-fixture",)]
    assert child.meta.kind == "subagent" and child.meta.label == "agent-fixture"
    assert child.meta.spawn_ordinal == 8 and child.meta.result_ordinal == 9
    assert child.turns[0].speaker == "system"
    source = next(s for s in adapter.discover(store) if s.session_id == "session-001")
    meta = adapter.session_meta(source)
    assert meta.project == "/home/synthetic/project" and meta.project_slug == "-home-synthetic-project"
    assert meta.title == "Synthetic parser investigation"
    assert meta.model == "claude-fixture" and meta.extra["branch"] == "fixture-branch"


def test_claude_incremental(tmp_path):
    location = tmp_path / "claude"
    shutil.copytree(FIXTURE, location)
    adapter = ClaudeAdapter()
    store = StoreSpec("claude", "claude", str(location))
    source = next(s for s in adapter.discover(store) if s.session_id == "session-001")
    for thread in source.threads:
        first = adapter.read_thread(source, thread, None)
        physical = Path(thread.location.partition("#sidechain=")[0])
        record = {"type": "user", "uuid": "appended-" + thread.kind,
                  "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "call-read", "content": "Recovered"}]}}
        if thread.kind == "sidechain":
            record.update(isSidechain=True, parentUuid="side-b")
        with physical.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        resumed = adapter.read_thread(source, thread, first.checkpoint)
        assert not resumed.reset and len(resumed.turns) == 1
        assert resumed.turns[0].ordinal == first.checkpoint.ordinal + 1
        assert resumed.turns[0].text == "Recovered"
        if thread.kind == "main":
            assert resumed.turns[0].tool == "Read"
        assert not adapter.read_thread(source, thread, resumed.checkpoint).turns
    physical = Path(source.root)
    physical.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "Restart"}}) + "\n")
    reset = adapter.read_thread(source, source.threads[0], first.checkpoint)
    assert reset.reset
    assert [(turn.ordinal, turn.text) for turn in reset.turns] == [(1, "Restart")]


def test_claude_question_answers_are_user_turns(tmp_path):
    location = tmp_path / "claude"
    shutil.copytree(FIXTURE, location)
    path = location / "-home-synthetic-project" / "session-002.jsonl"
    base = {"isSidechain": False, "sessionId": "session-002", "timestamp": "2026-08-01T12:01:00Z"}
    questions = [{"question": "Which rate?", "header": "Rate", "options": [{"label": "2%"}, {"label": "3%"}]},
                 {"question": "Who leads?", "header": "Lead", "options": [{"label": "Favour"}]}]
    rows = [
        {"type": "assistant", "uuid": "q-a", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion", "input": {"questions": questions}}]}},
        {"type": "user", "uuid": "q-u", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ask-1",
             "content": 'The user answered: "Which rate?"="2%". Read the answers carefully.'}]},
         "toolUseResult": {"questions": questions, "answers": {"Which rate?": "2%", "Who leads?": "Not sure yet, maybe Oge"},
                           "annotations": {"Who leads?": {"notes": "ask Favour first"}}}},
        {"type": "assistant", "uuid": "d-a", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "ask-2", "name": "AskUserQuestion", "input": {"questions": questions}}]}},
        {"type": "user", "uuid": "d-u", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ask-2", "is_error": True, "content": "The user declined to answer."}]}},
        {"type": "assistant", "uuid": "r-a", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "/x"}}]}},
        {"type": "user", "uuid": "r-u", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "read-1", "content": "file text"}]}},
    ]
    with path.open("a") as handle:
        for row in rows:
            handle.write(json.dumps({**base, **row}) + "\n")
    adapter = ClaudeAdapter()
    source = next(s for s in adapter.discover(StoreSpec("claude", "claude", str(location))) if s.session_id == "session-002")
    turns = adapter.read_thread(source, source.threads[0], None).turns
    assert [(t.speaker, t.tool, t.is_error) for t in turns[2:]] == [
        ("assistant", None, False), ("user", "AskUserQuestion", False),
        ("assistant", None, False), ("tool", "AskUserQuestion", True),
        ("assistant", None, False), ("tool", "Read", False)]
    answer = turns[3]
    assert answer.call_id == "ask-1"
    assert answer.text.splitlines() == ["2%  ← Which rate?",
                                        'Not sure yet, maybe Oge (note: {"notes":"ask Favour first"})  ← Who leads?']
    assert turns[5].text == "The user declined to answer."
