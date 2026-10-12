"""Build only this directory's synthetic Cursor metadata database and transcripts."""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent


def build():
    session = ROOT / "projects/home-synthetic-work/agent-transcripts/composer-main"
    child = session / "subagents/child-scout.jsonl"
    child.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "<timestamp>Wednesday, Sep 2, 2026, 2:43 PM (UTC-4)</timestamp>\n<user_query>\nInspect &amp; explain the sample.\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "Inspect the synthetic sample carefully."},
            {"type": "text", "text": "I will inspect the sample."},
            {"type": "tool_use", "id": "task-1", "name": "Task", "input": {"name": "SampleScout", "subagent_type": "scout", "prompt": "Inspect the sample\nReport the result."}},
            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"path": "/synthetic/sample.txt"}},
        ]}},
        {"type": "turn_ended", "status": "error", "error": "Synthetic read failed"},
        {"role": "user", "message": {"content": [{"type": "text", "text": "<system-reminder>This is injected context.</system-reminder>"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "Inspection complete."}]}},
        {"type": "turn_ended", "status": "success"},
    ]
    # Defensive thinking support is exercised above. Cursor has no verified
    # summary or tool-result record shape, so neither is fabricated here.
    (session / "composer-main.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    child.write_text(json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "The sample is synthetic."}]}}) + "\n")
    short = ROOT / "projects/empty-window/agent-transcripts/composer-short"
    short.mkdir(parents=True, exist_ok=True)
    (short / "composer-short.jsonl").write_text(json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "<timestamp>September 3, 2026, 09:10 AM (UTC+5:30)</timestamp><user_query>Hello.</user_query>"}]}}) + "\n")
    (ROOT / "projects/1775675906100").mkdir(parents=True, exist_ok=True)
    db = ROOT / "state.vscdb"
    db.unlink(missing_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE composerHeaders(composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, recency INTEGER, checkpointAt INTEGER, value TEXT)")
        conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO composerHeaders VALUES(?,?,?,?,?,?,?,?,?)", ("composer-main", "workspace-sample", 1788374580000, 1788374640000, 0, 0, 1, None, json.dumps({"name": "Synthetic sample review"})))
        conn.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:composer-main", json.dumps({"status": "completed", "subagentComposerIds": ["child-scout"], "subagentInfo": {"child-scout": {"name": "SampleScout"}}})))
        conn.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:child-scout", json.dumps({"name": "SampleScout", "status": "completed"})))


if __name__ == "__main__":
    build()
