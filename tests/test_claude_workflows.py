"""Current Claude Code transcripts: transport rows, queued prompts, titles and workflow runs."""
import json
from pathlib import Path

import pytest

from scry.adapters.base import StoreSpec
from scry.adapters.claude import ClaudeAdapter, _last_timestamp, _render
from scry.config import Config
from scry.index import Index
from scry.model import RefError, parse_ref
from scry.views import Renderer
from tests.adapter_contract import check_store

RUN = "wf_abc123-def"
SESSION = "session-wf"


def row(kind, uid, **fields):
    return {"type": kind, "uuid": uid, "timestamp": f"2026-11-05T10:00:{int(uid[1:]):02d}.000Z",
            "cwd": "/home/synthetic/project", **fields}


def lines(*rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


def build(root: Path, *, finished=True) -> Path:
    project = root / "-home-synthetic-project"
    session = project / SESSION
    run_dir = session / "subagents" / "workflows" / RUN
    scripts = session / "workflows" / "scripts"
    run_dir.mkdir(parents=True)
    scripts.mkdir(parents=True)
    ask = {"role": "user", "content": "study the thing"}
    launch = {"role": "assistant", "model": "m", "content": [
        {"type": "text", "text": "Launching."},
        {"type": "tool_use", "id": "t1", "name": "Workflow", "input": {"script": "export const meta = {}"}}]}
    (project / f"{SESSION}.jsonl").write_text(lines(
        {"type": "ai-title", "aiTitle": "Older title"},
        {"type": "api-request-blob", "sessionId": SESSION, "hash": "aa", "message": ask},
        row("user", "u1", message=ask),
        {"type": "api-request-blob", "sessionId": SESSION, "hash": "bb", "message": launch},
        row("assistant", "u2", message=launch),
        row("assistant", "u3", message={"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"}]}),
        row("user", "u4", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": f"Workflow launched\nTask ID: tk1\nRun ID: {RUN}\nMore"}]}),
        row("attachment", "u5", attachment={"type": "queued_command", "prompt": "also check the other thing",
                                            "humanTurn": True, "origin": {"kind": "human"}}),
        row("attachment", "u6", attachment={"type": "queued_command", "prompt": "<agent-message from=\"x\">done</agent-message>",
                                            "origin": {"kind": "agent"}}),
        row("attachment", "u7", attachment={"type": "date", "date": "2026-11-05"}),
        row("user", "u8", message={"role": "user", "content": "<task-notification>\n<task-id>other</task-id>\n"
                                                              f"<summary>checked {RUN}</summary>\n</task-notification>"}),
        row("user", "u9", message={"role": "user", "content": "<task-notification>\n<task-id>tk1</task-id>\n</task-notification>"}),
        row("user", "u10", message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}),
        row("user", "u11", message={"role": "user", "content": [{"type": "image", "source": {}}]}),
        {"type": "ai-title", "aiTitle": "Newest title"},
    ))
    (scripts / f"deep-study-{RUN}.js").write_text(
        "export const meta = {\n  name: 'deep-study',\n  description: 'Study it well', // why\n"
        "  phases: [\n    { title: 'Study' },\n    { title: 'Verify' },\n  ],\n}\n"
        "const SCHEMA = { description: 'not the meta', title: 'nope' }\n")
    (run_dir / "journal.jsonl").write_text(lines(
        {"type": "launched"},
        {"type": "started", "key": "k1", "agentId": "aaa", "label": "study:one", "phase": "Study"},
        {"type": "result", "key": "k1", "agentId": "aaa", "result": {"summary": "zebra finding"}},
        {"type": "started", "key": "k2", "agentId": "bbb", "label": "verify:one", "phase": "Verify"},
        {"type": "result", "key": "k2", "agentId": "bbb", "result": "quokka verdict"},
    ))
    for agent, label, phase in (("aaa", "study:one", "Study"), ("bbb", "verify:one", "Verify")):
        prompt = {"role": "user", "content": "[Workflow harness — user request] The harness relays"}
        (run_dir / f"agent-{agent}.jsonl").write_text(lines(
            row("user", "u1", agentId=agent, message=prompt),
            {"type": "api-request-blob", "hash": "cc", "message": prompt},
            row("assistant", "u2", agentId=agent, message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {"summary": "x"}}]})))
        (run_dir / f"agent-{agent}.meta.json").write_text(json.dumps(
            {"agentType": "workflow-subagent", "description": label, "workflowPhase": phase}))
    if finished:
        finish(session)
    return project / f"{SESSION}.jsonl"


def finish(session: Path) -> None:
    (session / "workflows" / f"{RUN}.json").write_text(json.dumps({
        "runId": RUN, "workflowName": "deep-study", "summary": "Study it well", "status": "completed",
        "agentCount": 2, "startTime": "2026-11-05T10:00:00.000Z", "logs": ["2/2 done"],
        "phases": [{"title": "Study"}, {"title": "Verify"}], "result": {"report": "narwhal conclusion"}}))


def read_all(location):
    adapter = ClaudeAdapter()
    store = StoreSpec("claude", "claude", str(location))
    source = next(s for s in adapter.discover(store) if s.session_id == SESSION)
    return adapter, source, {t.path: adapter.read_thread(source, t, None) for t in source.threads}


def test_transport_rows_are_not_turns_and_queued_prompts_are(tmp_path):
    build(tmp_path)
    adapter, source, reads = read_all(tmp_path)
    main = reads[()]
    assert [t.speaker for t in main.turns] == ["user", "assistant", "tool", "user", "system", "system", "system", "system", "user"]
    assert main.turns[-1].text == "[image]"
    assert main.turns[3].text == "also check the other thing"
    assert main.turns[4].text.startswith("<agent-message")
    assert all(t.ts is not None for t in main.turns)
    assert sum(c.name == "Workflow" for t in main.turns for c in t.calls) == 1
    assert adapter.session_meta(source).title == "Newest title"


def test_contract_holds_with_a_run(tmp_path):
    build(tmp_path)
    check_store(ClaudeAdapter(), StoreSpec("claude", "claude", str(tmp_path)))


def test_run_thread_and_its_agents(tmp_path):
    build(tmp_path)
    _, _, reads = read_all(tmp_path)
    assert set(reads) == {(), (RUN,), (RUN, "agent-aaa"), (RUN, "agent-bbb")}
    run = reads[(RUN,)]
    assert run.meta.kind == "workflow" and run.meta.agent_type == "deep-study" and run.meta.status == "completed"
    assert (run.meta.spawn_ordinal, run.meta.result_ordinal) == (2, 7)  # by task id, not by a mention
    assert [t.speaker for t in run.turns] == ["summary", "system", "assistant", "assistant", "assistant"]
    assert "phases: Study, Verify" in run.turns[0].text
    assert "study:one" in run.turns[2].text and "zebra finding" in run.turns[2].text
    assert "narwhal conclusion" in run.turns[-1].text and "2/2 done" in run.turns[-1].text
    child = reads[(RUN, "agent-aaa")]
    assert child.meta.task == "study:one" and child.meta.agent_type == "Study"
    assert [t.speaker for t in child.turns] == ["system", "assistant"]


def test_live_run_reads_from_its_script_and_gains_a_result_without_moving_turns(tmp_path):
    root = build(tmp_path, finished=False)
    adapter, source, reads = read_all(tmp_path)
    live = reads[(RUN,)]
    assert live.meta.status == "" and "Study it well" in live.turns[0].text
    assert "phases: Study, Verify" in live.turns[0].text and "not the meta" not in live.turns[0].text
    finish(root.with_suffix(""))
    source = next(s for s in adapter.discover(StoreSpec("claude", "claude", str(tmp_path))) if s.session_id == SESSION)
    thread = source.thread((RUN,))
    assert thread.fingerprint != live.checkpoint.fingerprint
    after = adapter.read_thread(source, thread, live.checkpoint)
    assert not after.reset and [t.ordinal for t in after.turns] == [live.turns[-1].ordinal + 1]
    assert "narwhal conclusion" in after.turns[0].text
    assert after.meta.status == "completed"


def test_journal_growing_past_a_final_result_starts_over(tmp_path):
    root = build(tmp_path)
    adapter, source, reads = read_all(tmp_path)
    done = reads[(RUN,)]
    journal = root.with_suffix("") / "subagents" / "workflows" / RUN / "journal.jsonl"
    with journal.open("a") as handle:
        handle.write(lines({"type": "result", "key": "k3", "agentId": "aaa", "result": "late"}))
    source = next(s for s in adapter.discover(StoreSpec("claude", "claude", str(tmp_path))) if s.session_id == SESSION)
    again = adapter.read_thread(source, source.thread((RUN,)), done.checkpoint)
    assert again.reset and len(again.turns) == len(done.turns) + 1


def config(tmp_path) -> Config:
    projects = tmp_path / "claude"
    projects.mkdir()
    build(projects)
    return Config(
        home=tmp_path, config_path=tmp_path / "config.toml", index_path=tmp_path / "index.sqlite3",
        export_dir=tmp_path / "exports", claude_projects_dir=projects, cursor_projects_dir=tmp_path / "none-cursor",
        cursor_state_db=tmp_path / "none.vscdb", omp_sessions_dir=tmp_path / "none-omp", hermes_dir=tmp_path / "none-hermes",
    )


def test_views_group_runs_and_results_are_found_by_default(tmp_path):
    cfg = config(tmp_path)
    index = Index(cfg)
    assert not index.run().errors
    views = Renderer(index, cfg)
    resolved = index.resolve(parse_ref(f"claude:{SESSION}"))
    tree = views.tree(resolved, 4000).text
    assert "2 agents · 1 workflows" in tree
    lines_ = tree.splitlines()
    at = next(i for i, l in enumerate(lines_) if l.strip().startswith(f"/{RUN}  workflow deep-study"))
    assert "agent-aaa" in lines_[at + 1] and "study:one" in lines_[at + 1]
    card = views.card(resolved, 4000).text
    assert "workflows (1):" in card and "agents (2, 2 in workflows)" in card
    assert index.resolve(parse_ref(f"claude:{SESSION}/agent-aaa")).thread_pk  # the pre-nesting ref still resolves
    prefix = index.resolve(parse_ref(f"claude:{SESSION}/wf_abc"))
    assert index.thread_row(prefix.thread_pk)["path"] == RUN
    index.close()


def test_a_new_adapter_version_rereads_only_its_store(tmp_path):
    index = Index(config(tmp_path))
    index.run()
    index.db.execute("UPDATE meta SET value='0' WHERE key='adapter:claude'")
    index.db.execute("UPDATE turn SET text='stale'")
    report = index.run()
    assert report.turns_added > 0
    assert index.db.execute("SELECT value FROM meta WHERE key='adapter:claude'").fetchone()[0] == ClaudeAdapter.version
    assert index.db.execute("SELECT COUNT(*) FROM turn WHERE text='stale'").fetchone()[0] == 0
    second = index.run()
    assert second.turns_added == 0
    index.close()


def test_a_killed_or_capped_upgrade_recovers_every_session(tmp_path):
    index = Index(config(tmp_path))
    index.run()
    before = index.db.execute("SELECT COUNT(*) FROM turn").fetchone()[0]
    index.db.execute("DELETE FROM meta WHERE key='adapter:claude'")
    report = index.run(time_cap=0)
    assert report.behind > 0 or index.db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
    index.run(time_cap=8.0)
    assert index.db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
    assert index.db.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == before
    index.close()


def test_a_run_prefix_prefers_the_run_but_nested_agents_stay_ambiguous(tmp_path):
    index = Index(config(tmp_path))
    index.run()
    assert index.thread_row(index.resolve(parse_ref(f"claude:{SESSION}/wf_abc")).thread_pk)["path"] == RUN
    index.db.execute("UPDATE thread SET kind='subagent' WHERE path=?", (RUN,))
    with pytest.raises(RefError):
        index.resolve(parse_ref(f"claude:{SESSION}/wf_abc"))
    index.close()


def test_failed_aborted_and_silent_runs_keep_what_they_recorded(tmp_path):
    root = build(tmp_path, finished=False)
    record = root.with_suffix("") / "workflows" / f"{RUN}.json"
    record.write_text(json.dumps({"status": "failed", "result": None, "error": "quota exceeded", "logs": ["died"], "agentCount": 2}))
    text = read_all(tmp_path)[2][(RUN,)].turns[-1].text
    assert "quota exceeded" in text and "died" in text and "null" not in text
    record.write_text(json.dumps({"status": "completed", "logs": ["only a log"], "agentCount": 2}))
    text = read_all(tmp_path)[2][(RUN,)].turns[-1].text
    assert "only a log" in text and "null" not in text


def test_a_failed_agent_shows_in_its_run(tmp_path):
    root = build(tmp_path)
    journal = root.with_suffix("") / "subagents" / "workflows" / RUN / "journal.jsonl"
    with journal.open("a") as handle:
        handle.write(lines({"type": "started", "key": "k3", "agentId": "ccc", "label": "study:two"},
                           {"type": "failed", "key": "k3", "agentId": "ccc"}))
    assert any(t.text == "study:two\nfailed" for t in read_all(tmp_path)[2][(RUN,)].turns)


def test_words_after_a_newline_in_a_structured_result_are_whole():
    assert _render({"summary": "alpha\nquokkaish beta", "n": [1, None]}) == "summary: alpha\nquokkaish beta\nn: 1\n"


def test_a_script_arriving_with_the_record_restarts_the_thread_instead_of_shifting_it(tmp_path):
    root = build(tmp_path, finished=False)
    session = root.with_suffix("")
    script = session / "workflows" / "scripts" / f"deep-study-{RUN}.js"
    text = script.read_text()
    script.unlink()  # launched from an earlier run's scriptPath
    adapter, _, reads = read_all(tmp_path)
    live = reads[(RUN,)]
    finish(session)
    record = session / "workflows" / f"{RUN}.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "script": text}))
    source = next(s for s in adapter.discover(StoreSpec("claude", "claude", str(tmp_path))) if s.session_id == SESSION)
    after = adapter.read_thread(source, source.thread((RUN,)), live.checkpoint)
    assert after.reset and [t.ordinal for t in after.turns] == list(range(1, 6))
    assert after.turns[1].speaker == "system"


def test_first_read_before_any_journal_row_restarts_rather_than_resends(tmp_path):
    root = build(tmp_path, finished=False)
    journal = root.with_suffix("") / "subagents" / "workflows" / RUN / "journal.jsonl"
    full = journal.read_text()
    journal.write_text("")
    adapter, _, reads = read_all(tmp_path)
    empty = reads[(RUN,)]
    assert empty.checkpoint.offset == 0
    journal.write_text(full)
    source = next(s for s in adapter.discover(StoreSpec("claude", "claude", str(tmp_path))) if s.session_id == SESSION)
    assert adapter.read_thread(source, source.thread((RUN,)), empty.checkpoint).reset


def background_agent(project: Path) -> Path:
    """A plain background agent: its tool result is a receipt, its notification the result."""
    session = project / "session-bg"
    (session / "subagents").mkdir(parents=True)
    prompt = "go look"
    (session / "subagents" / "agent-bg1.jsonl").write_text(lines(
        row("user", "u1", agentId="bg1", message={"role": "user", "content": prompt}),
        row("assistant", "u2", agentId="bg1", message={"role": "assistant", "content": "done"})))
    main = project / "session-bg.jsonl"
    main.write_text(lines(
        row("user", "u1", message={"role": "user", "content": "start"}),
        row("assistant", "u2", message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"prompt": prompt}}]}),
        row("user", "u3", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a1", "content": "Async agent launched"}]})))
    return main


def test_a_background_agents_result_is_its_notification_even_when_it_arrives_later(tmp_path):
    cfg = config(tmp_path)
    main = background_agent(cfg.claude_projects_dir / "-home-synthetic-project")
    index = Index(cfg)
    index.run()
    links = "SELECT spawn_ordinal, result_ordinal FROM thread WHERE path='agent-bg1'"
    assert tuple(index.db.execute(links).fetchone()) == (2, 3)
    with main.open("a") as handle:
        handle.write(lines(row("user", "u4", message={"role": "user", "content":
                               "<task-notification>\n<task-id>bg1</task-id>\n</task-notification>"})))
    index.run()
    assert tuple(index.db.execute(links).fetchone()) == (2, 4)
    assert index.run().threads_updated == 0  # and it settles: no re-read on every pass
    index.close()


def test_a_dangling_agent_or_session_file_does_not_stop_the_store(tmp_path):
    root = build(tmp_path)
    (root.with_suffix("") / "subagents" / "agent-gone.jsonl").symlink_to(tmp_path / "nowhere")
    (root.parent / "dangling.jsonl").symlink_to(tmp_path / "nowhere")
    ids = {s.session_id for s in ClaudeAdapter().discover(StoreSpec("claude", "claude", str(tmp_path)))}
    assert SESSION in ids


def test_a_session_that_failed_to_read_is_looked_for_again(tmp_path, monkeypatch):
    index = Index(config(tmp_path))
    index.run()
    index.db.execute("DELETE FROM meta WHERE key='adapter:claude'")
    real = ClaudeAdapter.session_meta
    monkeypatch.setattr(ClaudeAdapter, "session_meta", lambda self, source: (_ for _ in ()).throw(OSError("denied")))
    assert index.run().errors
    monkeypatch.setattr(ClaudeAdapter, "session_meta", real)
    assert not index.run().errors
    assert index.db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
    index.close()


def test_a_final_row_larger_than_the_tail_still_gives_its_time(tmp_path):
    path = tmp_path / "agent.jsonl"
    path.write_text(lines(row("assistant", "u9", message={"role": "assistant", "content": "x" * 70000})))
    assert _last_timestamp(path) == 1793872809.0


def test_a_quoted_notification_is_not_the_result(tmp_path):
    root = build(tmp_path)
    quoted = "<agent-message from=\"x\"><task-notification><task-id>tk1</task-id></task-notification></agent-message>"
    rows_ = root.read_text().splitlines(keepends=True)
    rows_.insert(6, lines(row("user", "u20", message={"role": "user", "content": quoted})))
    root.write_text("".join(rows_))
    run = read_all(tmp_path)[2][(RUN,)]
    assert run.meta.result_ordinal == 8


def test_threads_of_a_merged_stub_survive_a_scoped_pass_and_keep_their_links(tmp_path):
    cfg = config(tmp_path)
    project = cfg.claude_projects_dir / "-home-synthetic-project"
    stub_project = cfg.claude_projects_dir / "-home-synthetic-project-wt"
    stub_project.mkdir()
    (project / SESSION).rename(stub_project / SESSION)
    (stub_project / f"{SESSION}.jsonl").write_text(lines({"type": "agent-name", "agentName": "x"}))
    index = Index(cfg)
    assert not index.run().errors
    links = "SELECT spawn_ordinal, result_ordinal FROM thread WHERE path=?"
    assert tuple(index.db.execute(links, (RUN,)).fetchone()) == (2, 7)
    with (project / f"{SESSION}.jsonl").open("a") as handle:
        handle.write(lines(row("user", "u30", message={"role": "user", "content": "one more"})))
    index.run()
    assert tuple(index.db.execute(links, (RUN,)).fetchone()) == (2, 7)
    index.close()
