from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from scry.adapters.agy import AgyAdapter
from scry.adapters.base import StoreSpec, parse_timestamp
from tests.adapter_contract import check_store


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _build_fixture(root: Path) -> tuple[Path, Path]:
    brain = root / "brain"
    db_path = root / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id text PRIMARY KEY, title text, workspace_uris text, "
            "status text, agent_name text, parent_conversation_id text)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
            ("agy-main-001", "Synthetic Antigravity Session", "file:///work/synthetic",
             "CASCADE_RUN_STATUS_IDLE", "", ""),
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
            ("agy-sub-001", "", "file:///work/synthetic",
             "CASCADE_RUN_STATUS_IDLE", "research", "agy-main-001"),
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
            ("agy-sub-002", "", "file:///work/synthetic",
             "CASCADE_RUN_STATUS_IDLE", "self", "agy-sub-001"),
        )
        conn.commit()
    finally:
        conn.close()

    main_dir = brain / "agy-main-001"
    (main_dir / "plan.md").parent.mkdir(parents=True, exist_ok=True)
    (main_dir / "plan.md").write_text("# Implementation Plan\n", encoding="utf-8")
    (main_dir / "plan.md.metadata.json").write_text("{}", encoding="utf-8")
    (main_dir / "scratch").mkdir(parents=True, exist_ok=True)
    (main_dir / "scratch" / "probe.py").write_text("print('ok')\n", encoding="utf-8")

    _write_jsonl(main_dir / ".system_generated" / "logs" / "transcript_full.jsonl", [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:00Z",
            "content": (
                "<USER_REQUEST>\nInspect the sample project and delegate research.\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\nThe current local time is: 2026-10-29T10:00:00Z.\n</ADDITIONAL_METADATA>\n"
                "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Argon (Low).\n</USER_SETTINGS_CHANGE>"
            ),
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:05Z",
            "thinking": "I will read the skill and invoke a research subagent.",
            "content": "Starting inspection now.",
            "tool_calls": [
                {
                    "name": "read_file",
                    "args": {"Path": "/work/synthetic/skills/tidy/SKILL.md"},
                },
                {
                    "name": "invoke_subagent",
                    "args": {
                        "Model": "flash",
                        "Prompt": "Audit the module structure.",
                        "Role": "Module Auditor",
                        "TypeName": "research",
                    },
                },
            ],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:06Z",
            "content": "Showing lines 1-10 of 10\n# Tidy skill",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:07Z",
            "content": 'Created the following subagent:\n{\n  "conversationId": "agy-sub-001"\n}',
        },
        {
            "step_index": 4,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:20Z",
            "content": (
                "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
                "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T10:00:20Z sender=agy-sub-001 "
                "priority=MESSAGE_PRIORITY_HIGH content=Audit complete: all modules clean.\n</SYSTEM_MESSAGE>"
            ),
        },
        {
            "step_index": 5,
            "source": "SYSTEM",
            "type": "CHECKPOINT",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:25Z",
            "content": "# Resuming from a compaction\n\n<summary>\nCompacted summary of module audit.\n</summary>",
        },
        {
            "step_index": 6,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:30Z",
            "content": "Everything is verified.",
        },
    ])

    sub1_dir = brain / "agy-sub-001"
    _write_jsonl(sub1_dir / ".system_generated" / "logs" / "transcript_full.jsonl", [
        {
            "step_index": 0,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:08Z",
            "content": (
                "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T10:00:08Z sender=agy-main-001 "
                "priority=MESSAGE_PRIORITY_HIGH content=Audit the module structure.\n</SYSTEM_MESSAGE>"
            ),
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:10Z",
            "thinking": "Spawning a nested helper.",
            "tool_calls": [
                {
                    "name": "invoke_subagent",
                    "args": {
                        "Model": "flash_lite",
                        "Prompt": "Check leaf files.",
                        "Role": "Leaf Checker",
                        "TypeName": "self",
                    },
                },
            ],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:11Z",
            "content": 'Created the following subagent:\n{\n  "conversationId": "agy-sub-002"\n}',
        },
        {
            "step_index": 3,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:15Z",
            "content": (
                "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T10:00:15Z sender=agy-sub-002 "
                "priority=MESSAGE_PRIORITY_HIGH content=Leaves checked.\n</SYSTEM_MESSAGE>"
            ),
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:18Z",
            "tool_calls": [
                {
                    "name": "send_message",
                    "args": {"Message": "Audit complete: all modules clean.", "Recipient": "agy-main-001"},
                }
            ],
        },
        {
            "step_index": 5,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:19Z",
            "content": 'Message sent to "agy-main-001".',
        },
    ])

    sub2_dir = brain / "agy-sub-002"
    _write_jsonl(sub2_dir / ".system_generated" / "logs" / "transcript.jsonl", [
        {
            "step_index": 0,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:12Z",
            "content": (
                "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T10:00:12Z sender=agy-sub-001 "
                "priority=MESSAGE_PRIORITY_HIGH content=Check leaf files.\n</SYSTEM_MESSAGE>"
            ),
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-10-29T10:00:14Z",
            "content": "Leaves checked.",
        },
    ])
    return brain, db_path


def test_agy_store_and_subagents(tmp_path: Path) -> None:
    brain, db_path = _build_fixture(tmp_path)
    adapter = AgyAdapter()
    store = adapter.stores(SimpleNamespace(agy_brain_dir=brain, agy_summaries_db=db_path))[0]
    report = check_store(adapter, store)

    assert set(report.sessions) == {"agy-main-001"}
    tree = report.sessions["agy-main-001"]
    assert set(tree) == {
        (),
        ("Module-Auditor",),
        ("Module-Auditor", "Leaf-Checker"),
    }

    main = tree[()].turns
    assert [t.speaker for t in main] == ["user", "assistant", "tool", "tool", "mail", "summary", "assistant"]
    assert main[0].text == "Inspect the sample project and delegate research."
    assert main[0].ts == parse_timestamp("2026-10-29T10:00:00Z")
    assert [c.name for c in main[1].calls] == ["read_file", "invoke_subagent"]
    assert main[1].calls[1].intent == "Module Auditor"
    assert (main[2].tool, main[2].call_id) == ("read_file", "step-1-0")
    assert (main[3].tool, main[3].call_id) == ("invoke_subagent", "step-1-1")
    assert main[4].text == "from agy:agy-sub-001: Audit complete: all modules clean."
    assert main[5].text == "Compacted summary of module audit."

    auditor = tree[("Module-Auditor",)].meta
    assert (auditor.kind, auditor.label, auditor.agent_type) == ("subagent", "Module Auditor", "research")
    assert (auditor.task, auditor.model) == ("Audit the module structure.", "flash")
    assert (auditor.spawn_ordinal, auditor.result_ordinal) == (2, 5)

    leaf = tree[("Module-Auditor", "Leaf-Checker")].meta
    assert (leaf.kind, leaf.label, leaf.agent_type) == ("subagent", "Leaf Checker", "self")
    assert (leaf.task, leaf.model) == ("Check leaf files.", "flash_lite")
    assert (leaf.spawn_ordinal, leaf.result_ordinal) == (2, 4)

    source = next(adapter.discover(store))
    meta = adapter.session_meta(source)
    assert meta.title == "Synthetic Antigravity Session"
    assert meta.project == "/work/synthetic"
    assert meta.model == "Gemini 4 Argon (Low)"
    assert meta.native_handle == "conversation://agy-main-001"
    assert {(a.name, a.kind) for a in meta.artifacts} == {
        ("plan.md", "output"),
        ("scratch/probe.py", "other"),
    }


def test_agy_incremental(tmp_path: Path) -> None:
    brain, db_path = _build_fixture(tmp_path)
    adapter = AgyAdapter()
    store = StoreSpec("agy", "agy", str(brain), True, metadata=str(db_path))
    source = next(adapter.discover(store))
    thread = source.thread(())
    assert thread is not None
    full = adapter.read_thread(source, thread, None)

    path = Path(thread.location)
    appended_row = {
        "step_index": 7,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-10-29T10:01:00Z",
        "content": "<USER_REQUEST>\nFollow-up question.\n</USER_REQUEST>",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(appended_row) + "\n")

    resumed = adapter.read_thread(source, thread, full.checkpoint)
    assert not resumed.reset
    assert [(t.ordinal, t.speaker, t.text) for t in resumed.turns] == [
        (full.checkpoint.ordinal + 1, "user", "Follow-up question.")
    ]
    assert not adapter.read_thread(source, thread, resumed.checkpoint).turns


def test_agy_stress_edge_cases(tmp_path: Path) -> None:
    """Stress test canceled background tasks, GENERIC trailing SYSTEM_MESSAGE stripping,
    duplicate RUNNING steps, DB-less subagent fallback, role collision deduplication,
    and Windows URI normalization."""
    brain = tmp_path / "brain"
    main_dir = brain / "agy-stress-root"
    _write_jsonl(main_dir / ".system_generated" / "logs" / "transcript_full.jsonl", [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-10-30T12:00:00Z",
            "content": "<USER_REQUEST>\nRun two parallel researchers and cancel a slow task.\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-10-30T12:00:02Z",
            "tool_calls": [
                {
                    "name": "invoke_subagent",
                    "args": {"Model": "inherit", "Prompt": "First worker task.", "Role": "Worker", "TypeName": "research"},
                },
                {
                    "name": "invoke_subagent",
                    "args": {"Model": "pro", "Prompt": "Second worker task.", "Role": "Worker", "TypeName": "research"},
                },
                {
                    "name": "edit_file",
                    "args": {"Path": "C:\\work\\demo.py", "Target": "a", "Replacement": "b"},
                },
            ],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-10-30T12:00:03Z",
            "content": 'Created the following subagent:\n{\n  "conversationId": "agy-worker-1"\n}',
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-10-30T12:00:03Z",
            "content": 'Created the following subagent:\n{\n  "conversationId": "agy-worker-1"\n}',
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-10-30T12:00:04Z",
            "content": 'Created the following subagent:\n{\n  "conversationId": "agy-worker-2"\n}',
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-10-30T12:00:05Z",
            "content": (
                "Made 1 replacement in C:\\work\\demo.py.\n\n"
                "The following is a <SYSTEM_MESSAGE> not actually sent by the user. "
                "It is provided by the system as important information to pay attention to.\n\n"
                "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-30T12:00:05Z "
                "sender=agy-stress-root/task-99 priority=MESSAGE_PRIORITY_LOW "
                'content=Task id "agy-stress-root/task-99" was canceled with result:\n'
                "Tool execution was canceled\n</SYSTEM_MESSAGE>"
            ),
        },
        {
            "step_index": 5,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-10-30T12:00:06Z",
            "content": (
                "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
                "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-30T12:00:05Z "
                "sender=agy-stress-root/task-99 priority=MESSAGE_PRIORITY_LOW "
                'content=Task id "agy-stress-root/task-99" was canceled with result:\n'
                "Tool execution was canceled\n</SYSTEM_MESSAGE>"
            ),
        },
    ])

    for cid, prompt in (("agy-worker-1", "First worker task."), ("agy-worker-2", "Second worker task."), ("agy-orphan-child", "Recovered via step 0 sender.")):
        _write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript.jsonl", [
            {
                "step_index": 0,
                "source": "SYSTEM",
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "created_at": "2026-10-30T12:00:07Z",
                "content": (
                    "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-30T12:00:07Z "
                    f"sender=agy-stress-root priority=MESSAGE_PRIORITY_HIGH content={prompt}\n</SYSTEM_MESSAGE>"
                ),
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-10-30T12:00:08Z",
                "content": "Done.",
            },
        ])

    # Notice: no SQLite DB passed at all! Everything must resolve from transcripts alone.
    adapter = AgyAdapter()
    store = StoreSpec("agy", "agy", str(brain), True)
    report = check_store(adapter, store)
    assert set(report.sessions) == {"agy-stress-root"}
    tree = report.sessions["agy-stress-root"]
    assert set(tree) == {
        (),
        ("Worker",),
        ("Worker-agy-work",),
        ("sub-agy-orph",),
    }
    main_turns = tree[()].turns
    # Duplicate RUNNING step_index=2 deduplicated; trailing <SYSTEM_MESSAGE> stripped from step 4;
    # canceled task at step 5 classified as tool (background_task, is_error=True), not mail!
    assert [t.speaker for t in main_turns] == ["user", "assistant", "tool", "tool", "tool", "tool"]
    assert main_turns[4].tool == "edit_file"
    assert "<SYSTEM_MESSAGE>" not in main_turns[4].text
    assert main_turns[5].tool == "background_task"
    assert main_turns[5].call_id == "agy-stress-root/task-99"
    assert main_turns[5].is_error is True

