"""Build a wholly synthetic Hermes bank using the observed Hermes schema."""
import json
import sqlite3
from pathlib import Path

SCHEMA = 'CREATE TABLE sessions (\n    id TEXT PRIMARY KEY,\n    source TEXT NOT NULL,\n    user_id TEXT,\n    model TEXT,\n    model_config TEXT,\n    system_prompt TEXT,\n    parent_session_id TEXT,\n    started_at REAL NOT NULL,\n    ended_at REAL,\n    end_reason TEXT,\n    message_count INTEGER DEFAULT 0,\n    tool_call_count INTEGER DEFAULT 0,\n    input_tokens INTEGER DEFAULT 0,\n    output_tokens INTEGER DEFAULT 0,\n    cache_read_tokens INTEGER DEFAULT 0,\n    cache_write_tokens INTEGER DEFAULT 0,\n    reasoning_tokens INTEGER DEFAULT 0,\n    billing_provider TEXT,\n    billing_base_url TEXT,\n    billing_mode TEXT,\n    estimated_cost_usd REAL,\n    actual_cost_usd REAL,\n    cost_status TEXT,\n    cost_source TEXT,\n    pricing_version TEXT,\n    title TEXT, "api_call_count" INTEGER DEFAULT 0, "handoff_state" TEXT, "handoff_platform" TEXT, "handoff_error" TEXT, "cwd" TEXT, "rewind_count" INTEGER NOT NULL DEFAULT 0, "archived" INTEGER NOT NULL DEFAULT 0, "session_key" TEXT, "chat_id" TEXT, "chat_type" TEXT, "thread_id" TEXT, "git_branch" TEXT, "git_repo_root" TEXT, "compression_failure_cooldown_until" REAL, "compression_failure_error" TEXT, "display_name" TEXT, "origin_json" TEXT, "expiry_finalized" INTEGER DEFAULT 0, "compression_fallback_streak" INTEGER NOT NULL DEFAULT 0, "profile_name" TEXT, "compression_ineffective_count" INTEGER NOT NULL DEFAULT 0, "pinned" INTEGER NOT NULL DEFAULT 0, "system_prompt_hash" TEXT, "last_activity_at" REAL, "last_activity_description" TEXT, "last_activity_provenance" TEXT, "last_read_at" REAL, "title_source" TEXT, "git_metadata_generation" INTEGER NOT NULL DEFAULT 0, "hidden" INTEGER NOT NULL DEFAULT 0,\n    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)\n);\nCREATE TABLE messages (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    session_id TEXT NOT NULL REFERENCES sessions(id),\n    role TEXT NOT NULL,\n    content TEXT,\n    tool_call_id TEXT,\n    tool_calls TEXT,\n    tool_name TEXT,\n    timestamp REAL NOT NULL,\n    token_count INTEGER,\n    finish_reason TEXT,\n    reasoning TEXT,\n    reasoning_details TEXT,\n    codex_reasoning_items TEXT\n, "reasoning_content" TEXT, "codex_message_items" TEXT, "platform_message_id" TEXT, "observed" INTEGER DEFAULT 0, "active" INTEGER NOT NULL DEFAULT 1, "compacted" INTEGER NOT NULL DEFAULT 0, "effect_disposition" TEXT, "api_content" TEXT, "display_kind" TEXT, "display_metadata" TEXT, "_compressed_summary" INTEGER NOT NULL DEFAULT 0);\nCREATE TABLE async_delegations (\n    delegation_id TEXT PRIMARY KEY,\n    origin_session TEXT NOT NULL,\n    origin_ui_session_id TEXT NOT NULL DEFAULT \'\',\n    parent_session_id TEXT,\n    state TEXT NOT NULL,\n    dispatched_at REAL NOT NULL,\n    completed_at REAL,\n    updated_at REAL NOT NULL,\n    event_json TEXT,\n    result_json TEXT,\n    delivery_state TEXT NOT NULL DEFAULT \'pending\',\n    delivery_attempts INTEGER NOT NULL DEFAULT 0,\n    delivered_at REAL,\n    owner_pid INTEGER,\n    owner_started_at INTEGER,\n    task_json TEXT,\n    delivery_claim TEXT,\n    delivery_claimed_at REAL\n, origin_session_id TEXT);'


def build(path=Path(__file__).with_name("state.db")):
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.executescript("CREATE INDEX idx_messages_session ON messages(session_id,timestamp); CREATE INDEX idx_sessions_parent ON sessions(parent_session_id); CREATE INDEX idx_delegations_parent ON async_delegations(parent_session_id);")
        for sid, parent, source, title in (("root-demo", None, "cli", "Synthetic garden plan"), ("child-demo", "root-demo", "delegate", None), ("grandchild-demo", "child-demo", "delegate", None), ("second-demo", None, "cli", "Second synthetic session")):
            db.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,ended_at,end_reason,cwd,title,model) VALUES (?,?,?,1700000000,1700000060,'complete','/home/example/garden',?,'fixture-model')", (sid,source,parent,title))
        def message(sid, role, content, **fields):
            values = dict(session_id=sid, role=role, content=content, timestamp=1700000001, **fields)
            db.execute(f"INSERT INTO messages({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        message("root-demo", "user", "Plan a synthetic garden.")
        calls = [{"id":"call-delegate","type":"function","function":{"name":"delegate_task","arguments":json.dumps({"goal":"Inspect soil","role":"leaf"})}}, {"id":"call-read","type":"function","function":{"name":"read_file","arguments":json.dumps({"path":"missing.txt"})}}]
        message("root-demo", "assistant", "I will inspect the soil and notes.", reasoning="Compare drainage options.", tool_calls=json.dumps(calls))
        message("root-demo", "tool", json.dumps({"delegation_id":"delegation-demo","status":"running"}), tool_name="delegate_task", tool_call_id="call-delegate")
        message("root-demo", "tool", json.dumps({"error":"Synthetic missing file"}), tool_name="read_file", tool_call_id="call-read")
        message("root-demo", "system", "Synthetic injected context.")
        message("root-demo", "assistant", "Earlier plan summarized.", _compressed_summary=1)
        message("root-demo", "assistant", "An ordinary reply later folded by compaction.", compacted=1)
        message("root-demo", "system", json.dumps({"type":"async_delegation","delegation_id":"delegation-demo","status":"completed","results":[{"summary":"Soil is suitable."}]}))
        message("child-demo", "user", "Inspect soil")
        message("child-demo", "assistant", "Soil is suitable.")
        message("grandchild-demo", "user", "Check drainage")
        message("grandchild-demo", "assistant", "Drainage is adequate.")
        message("second-demo", "user", "Name one herb.")
        message("second-demo", "assistant", "Basil.")
        event = dict(type="async_delegation", delegation_id="delegation-demo", parent_session_id="root-demo", goal="Inspect soil", role="leaf", status="completed", results=[{"summary":"Soil is suitable."}])
        db.execute("INSERT INTO async_delegations(delegation_id,origin_session,parent_session_id,state,dispatched_at,completed_at,updated_at,event_json,result_json) VALUES ('delegation-demo','root-demo','root-demo','completed',1700000002,1700000050,1700000050,?,?)", (json.dumps(event),json.dumps({"results":event["results"]})))


if __name__ == "__main__":
    build()
