"""The skills view: invocations and skill reads counted per skill from the index.

Two OMP sessions are written into a temporary store. The first holds a skill
invocation before its first turn, skill reads by ``skill://`` and by ``SKILL.md``
path, reads and calls that are not skill uses, a second invocation later, and a
Scout subagent reading two skills. The second, later session reads one skill.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scry import cli
from scry.config import Config
from scry.index import Index, skill_of_call
from scry.model import parse_ref
from scry.views import Renderer

REF_RE = re.compile(r"ref=([^\s()]+)")
SINCE_SECOND = 1790035200.0  # 2026-09-22T00:00Z, between the two sessions


def _message(mid: str, ts: str, role: str, content, **extra) -> dict:
    return {"type": "message", "id": mid, "timestamp": ts, "message": {"role": role, "content": content, "timestamp": ts, **extra}}


def _call(cid: str, name: str, arguments: dict) -> dict:
    return {"type": "toolCall", "id": cid, "name": name, "arguments": arguments}


def _result(mid: str, ts: str, cid: str, name: str) -> dict:
    return _message(mid, ts, "toolResult", [{"type": "text", "text": "ok"}], toolCallId=cid, toolName=name, isError=False)


def _skill_prompt(sid: str, ts: str, name: str, args: str) -> dict:
    return {"type": "custom_message", "customType": "skill-prompt", "display": True, "attribution": "user", "id": sid, "timestamp": ts,
            "content": f"[IMPORTANT: User invoked the \"{name}\" skill.]\n\nbody\n\nUser: {args}",
            "details": {"name": name, "path": f"/skills/{name}/SKILL.md", "args": args, "lineCount": 3}}


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _build_store(root: Path) -> None:
    project = root / "-work-skills"
    first = "2026-09-20T10-00-00-000Z_fixskill1"
    _write(project / f"{first}.jsonl", [
        {"type": "session", "version": 3, "id": "fixskill1", "cwd": "/work/skills", "timestamp": "2026-09-20T10:00:00Z"},
        _skill_prompt("sk1", "2026-09-20T10:00:10Z", "grilling", "grill the plan"),  # @0.1
        _message("u1", "2026-09-20T10:01:00Z", "user", [{"type": "text", "text": "Look at the plan."}]),  # @1
        _message("a1", "2026-09-20T10:02:00Z", "assistant", [  # @2
            {"type": "text", "text": "Reading."},
            _call("r1", "read", {"path": "skill://retro"}),
            _call("r2", "read", {"path": "/home/me/.omp/agent/skills/deep-design/SKILL.md:1-40"}),
            _call("r3", "read", {"path": "README.md"}),
            _call("b1", "bash", {"command": "cat skill://retro/SKILL.md"}),
        ]),
        _result("t1", "2026-09-20T10:02:01Z", "r1", "read"),
        _result("t2", "2026-09-20T10:02:02Z", "r2", "read"),
        _result("t3", "2026-09-20T10:02:03Z", "r3", "read"),
        _result("t4", "2026-09-20T10:02:04Z", "b1", "bash"),  # @6
        _skill_prompt("sk2", "2026-09-20T10:05:00Z", "retro", "wrap up"),  # @6.1
    ])
    _write(project / first / "Scout.jsonl", [
        {"type": "session", "version": 3, "id": "child-scout", "cwd": "/work/skills", "timestamp": "2026-09-20T11:00:00Z"},
        {"type": "session_init", "agent": "scout", "task": "Check skills", "modelRole": "smol", "readOnly": True},
        _message("cu", "2026-09-20T11:00:00Z", "user", "Check the skills.", attribution="parent"),  # @1
        _message("ca", "2026-09-20T11:01:00Z", "assistant", [  # @2
            _call("s1", "read", {"path": "skill://retro/references/notes.md"}),
            _call("s2", "read", {"path": "/work/skills/grilling/SKILL.md"}),
        ]),
        _result("ct1", "2026-09-20T11:01:01Z", "s1", "read"),
        _result("ct2", "2026-09-20T11:01:02Z", "s2", "read"),
    ])
    _write(project / "2026-09-25T10-00-00-000Z_fixskill2.jsonl", [
        {"type": "session", "version": 3, "id": "fixskill2", "cwd": "/work/skills", "timestamp": "2026-09-25T10:00:00Z"},
        _message("u1", "2026-09-25T10:01:00Z", "user", [{"type": "text", "text": "Again."}]),
        _message("a1", "2026-09-25T10:02:00Z", "assistant", [_call("r1", "read", {"path": "skill://retro"})]),  # @2
        _result("t1", "2026-09-25T10:02:01Z", "r1", "read"),
    ])


@pytest.fixture(scope="module")
def cfg(tmp_path_factory) -> Config:
    tmp = tmp_path_factory.mktemp("skills")
    _build_store(tmp / "omp")
    return Config(
        home=tmp, config_path=tmp / "config.toml", index_path=tmp / "index.sqlite3", export_dir=tmp / "exports",
        claude_projects_dir=tmp / "no-claude", cursor_projects_dir=tmp / "no-cursor", cursor_state_db=tmp / "no-state",
        omp_sessions_dir=tmp / "omp", hermes_dir=tmp / "no-hermes",
    )


@pytest.fixture(scope="module")
def index(cfg) -> Index:
    idx = Index(cfg)
    report = idx.run()
    assert not report.errors, report.errors
    yield idx
    idx.close()


def _by_name(view) -> dict[str, dict]:
    return {s["name"]: s for s in view.meta["skills"]}


def _view(index: Index, cfg: Config, *, since: float | None = None, budget: int = 4000):
    entries = index.skills(stores=("omp",), project=None, since=since, until=None)
    return Renderer(index, cfg).skills(entries, budget, total=len(entries), total_uses=sum(e["uses"] for e in entries),
                                       since_ts=since, until_ts=None, stores=("omp",))


def test_counts_invocations_and_reads_per_skill_and_thread(index, cfg):
    view = _view(index, cfg)
    assert view.kind == "skills"
    assert [s["name"] for s in view.meta["skills"]] == ["retro", "grilling", "deep-design"]
    skills = _by_name(view)
    assert {k: skills["retro"][k] for k in ("uses", "invoked", "read", "sessions")} == {"uses": 4, "invoked": 1, "read": 3, "sessions": 2}
    assert skills["retro"]["threads"] == {"main": 3, "subagent": 1, "advisor": 0, "sidechain": 0}
    assert {k: skills["grilling"][k] for k in ("uses", "invoked", "read", "sessions")} == {"uses": 2, "invoked": 1, "read": 1, "sessions": 1}
    assert skills["grilling"]["threads"] == {"main": 1, "subagent": 1, "advisor": 0, "sidechain": 0}
    assert {k: skills["deep-design"][k] for k in ("uses", "invoked", "read", "sessions")} == {"uses": 1, "invoked": 0, "read": 1, "sessions": 1}
    for s in skills.values():
        assert set(s) == {"name", "uses", "invoked", "read", "sessions", "threads", "first_ts", "last_ts", "last_ref"}
        assert s["first_ts"] <= s["last_ts"]
    assert set(view.meta) >= {"skills", "since_ts", "until_ts", "stores"} and view.meta["stores"] == ["omp"]


def test_last_ref_points_at_the_newest_use(index, cfg):
    skills = _by_name(_view(index, cfg))
    retro = index.resolve(parse_ref(skills["retro"]["last_ref"]))
    assert (retro.ref.session, retro.turns, retro.sub) == ("fixskill2", (2, 2), None)
    grilling = index.resolve(parse_ref(skills["grilling"]["last_ref"]))
    assert (grilling.ref.session, grilling.thread_path, grilling.turns) == ("fixskill1", "Scout", (2, 2))
    deep = index.resolve(parse_ref(skills["deep-design"]["last_ref"]))
    assert (deep.thread_path, deep.turns) == ("", (2, 2))


def test_since_bounds_the_uses(index, cfg):
    view = _view(index, cfg, since=SINCE_SECOND)
    assert [(s["name"], s["uses"], s["read"], s["sessions"]) for s in view.meta["skills"]] == [("retro", 1, 1, 1)]


@pytest.mark.parametrize("budget", [200, 400, 4000])
def test_text_fits_budget_and_every_ref_resolves(index, cfg, budget):
    view = _view(index, cfg, budget=budget)
    assert len(view.text) <= budget and "next:" in view.text
    for token in REF_RE.findall(view.text):
        if "<" not in token:
            index.resolve(parse_ref(token.rstrip(".,;:")))


def test_cli_json_envelope(cfg, index, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["--json", "skills", "--store", "omp", "--limit", "2", "--no-index"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == {"schema", "view", "text", "refs", "meta"} and envelope["view"] == "skills"
    assert [s["name"] for s in envelope["meta"]["skills"]] == ["retro", "grilling"]
    assert envelope["meta"]["since_ts"] is None and envelope["meta"]["stores"] == ["omp"]
    assert cli.main(["--json", "--view", "skills", "--since", "2026-09-22", "--no-index"]) == 0
    assert [s["name"] for s in json.loads(capsys.readouterr().out)["meta"]["skills"]] == ["retro"]


def test_skill_of_call_across_harnesses():
    assert skill_of_call("Skill", json.dumps({"skill": "pdf"})) == ("pdf", "invoked")
    assert skill_of_call("Skill", json.dumps({"command": "/pdf"})) == ("pdf", "invoked")
    assert skill_of_call("skill_view", json.dumps({"name": "writing-plans"})) == ("writing-plans", "read")
    assert skill_of_call("read_file", json.dumps({"path": "/h/.hermes/skills/ops/runbook/SKILL.md", "limit": 50})) == ("runbook", "read")
    assert skill_of_call("read_file", json.dumps({"Path": r"C:\Users\me\.gemini\antigravity\builtin\skills\antigravity_guide\SKILL.md"})) == ("antigravity_guide", "read")
    assert skill_of_call("Read", json.dumps({"file_path": "/repo/.claude/skills/tidy/SKILL.md"})) == ("tidy", "read")
    assert skill_of_call("read", json.dumps({"path": "skill://scry:10-20"})) == ("scry", "read")
    assert skill_of_call("read", json.dumps({"path": "/repo/docs/SKILL.md.bak"})) is None
    assert skill_of_call("read", json.dumps({"path": "/repo/README.md"})) is None
    assert skill_of_call("bash", json.dumps({"command": "cat skill://x"})) is None
    assert skill_of_call("Skill", "not json") is None
