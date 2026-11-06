"""Project exclusions scope discovery, not indexing or explicit references."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scry import cli, query
from scry.config import Config
from scry.index import Index
from scry.model import parse_ref
from scry.query import Scope, find


def _session(root: Path, sid: str, project: str | None, text: str, *, skill: str = "shared") -> None:
    # A bare '-' slug decodes to None when the transcript has no cwd.
    path = root / "-" / f"2026-11-06T00-00-00-000Z_{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    rows = [
        {"type": "session", "version": 3, "id": sid, "cwd": project, "timestamp": ts},
        {"type": "message", "id": "u1", "timestamp": ts,
         "message": {"role": "user", "content": text, "timestamp": ts}},
        {"type": "message", "id": "a1", "timestamp": ts,
         "message": {"role": "assistant", "timestamp": ts, "content": [
             {"type": "toolCall", "id": "read1", "name": "read", "arguments": {"path": f"skill://{skill}"}},
         ]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def _config(tmp: Path) -> Config:
    return Config(
        home=tmp, config_path=tmp / "config.toml", index_path=tmp / "index.sqlite3", export_dir=tmp / "exports",
        claude_projects_dir=tmp / "no-claude", cursor_projects_dir=tmp / "no-cursor", cursor_state_db=tmp / "no-state",
        omp_sessions_dir=tmp / "omp", hermes_dir=tmp / "no-hermes",
    )


@pytest.fixture
def corpus(tmp_path):
    cfg = _config(tmp_path)
    for sid, project, text in [
        ("unknown", None, "needle publicword plus some extra context"),
        ("personal", "/home/me/personal", "needle publicword plus some extra context"),
        ("visible", "/home/me/workshop", "needle publicword plus some extra context"),
        ("hidden", "/home/me/work", "needle secretword"),
        ("nested", "/home/me/work/app", "needle secretword"),
    ]:
        _session(cfg.omp_sessions_dir, sid, project, text, skill="hiddenonly" if sid == "nested" else "shared")
    idx = Index(cfg)
    report = idx.run()
    assert not report.errors, report.errors
    yield cfg, idx
    idx.close()


@pytest.fixture
def invoke(corpus, monkeypatch, capsys):
    cfg, _ = corpus
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    def call(*args):
        assert cli.main(["--json", "--no-index", "--budget", "20000", *args]) == 0
        return json.loads(capsys.readouterr().out)

    return call


def _ids(index, **kwargs):
    rows = index.sessions(stores=None, project=None, since=None, until=None, limit=100, **kwargs)
    return {row["session_id"] for row in rows}


def _ref_ids(envelope):
    return {parse_ref(ref).session for ref in envelope["refs"]}


def test_repeated_cli_exclusions_combine_with_existing_project_inclusion(invoke):
    result = invoke("sessions", "--project", "/home/me", "--exclude-project", "work", "--exclude-project=personal")
    assert _ref_ids(result) == {"visible"}
    # Repetition must retain the first value as well as the last; unknown paths
    # stay visible unless a separate positive project filter removes them.
    assert _ref_ids(invoke("sessions", "--exclude-project=work", "--exclude-project", "personal")) == {"visible", "unknown"}
    found = invoke("needle", "--exclude-project=work", "--exclude-project", "personal")
    assert found["view"] == "find" and _ref_ids(found) == {"visible", "unknown"}


def test_discovery_filters_before_list_limit_and_search_candidate_limit(corpus, invoke, monkeypatch):
    _, idx = corpus
    assert _ids(idx, exclude_projects=("work",)) == {"visible", "personal", "unknown"}
    assert _ref_ids(invoke("sessions", "--exclude-project", "workshop", "--exclude-project", "personal", "--exclude-project", "work", "--limit", "1")) == {"unknown"}
    # Keep the fixture small while exercising the actual SQL candidate boundary.
    monkeypatch.setattr(query, "CANDIDATES", 1)
    assert find(idx, "needle", limit=1).groups[0].session_id in {"hidden", "nested"}
    result = find(idx, "needle", scope=Scope(exclude_projects=("work", "personal")), limit=1)
    assert len(result.groups) == 1 and result.groups[0].session_id in {"visible", "unknown"}
    assert result.total_hits == 1


def test_query_fallback_and_term_counts_stay_inside_scope(corpus):
    _, idx = corpus
    result = find(idx, "publicword secretword", scope=Scope(exclude_projects=("work",)))
    assert result.tier == "any"
    assert {group.session_id for group in result.groups} == {"visible", "personal", "unknown"}
    assert result.per_term == {"publicword": 3, "secretword": 0}
    missing = find(idx, "secretword", scope=Scope(exclude_projects=("work",)))
    assert not missing.groups and missing.per_term == {"secretword": 0}


def test_discovery_overview_projects_live_and_skills(invoke):
    overview = invoke("overview", "--exclude-project", "work")
    assert "3 sessions" in overview["text"] and "6 turns" in overview["text"]
    assert "/home/me/work " not in overview["text"] and "/home/me/work/app" not in overview["text"]
    assert _ref_ids(overview) == {"visible", "personal", "unknown"}
    projects = invoke("projects", "--exclude-project", "work")
    assert "/home/me/workshop" in projects["text"] and "/home/me/personal" in projects["text"]
    assert "/home/me/work " not in projects["text"] and "/home/me/work/app" not in projects["text"]
    assert _ref_ids(invoke("live", "--exclude-project", "work")) == {"visible", "personal", "unknown"}
    skills = invoke("skills", "--exclude-project", "work")["meta"]["skills"]
    assert len(skills) == 1 and skills[0]["name"] == "shared"
    assert skills[0]["uses"] == skills[0]["sessions"] == 3
    assert parse_ref(skills[0]["last_ref"]).session in {"visible", "personal", "unknown"}


def test_plain_word_does_not_probe_excluded_session_but_explicit_refs_work(corpus, invoke):
    _, idx = corpus
    result = invoke("hidden", "--exclude-project", "work")
    assert result["view"] == "find" and not result["refs"]
    card = invoke("omp:hidden", "--exclude-project", "work")
    assert card["view"] == "card" and _ref_ids(card) == {"hidden"}
    read = invoke("omp:hidden@1", "--exclude-project", "work")
    assert read["view"] == "read" and "secretword" in read["text"]
    assert idx.resolve(parse_ref("omp:hidden")).turn_count == 2
    assert _ids(idx) == {"hidden", "nested", "visible", "personal", "unknown"}


def test_exclusion_paths_are_literal_case_sensitive_and_component_bounded(tmp_path):
    cfg = _config(tmp_path)
    projects = {
        "base": "/home/me/work", "child": "/home/me/work/app", "similar": "/home/me/workshop",
        "elsewhere": "/srv/work/app", "upper": "/home/me/Work", "percent": "/home/me/work%",
        "percentchild": "/home/me/work%/app", "underscore": "/home/me/work_", "wildcardlookalike": "/home/me/workX",
        "windows": r"C:\Users\me\work", "windowschild": r"C:\Users\me\work\app",
        "windowssimilar": r"C:\Users\me\workshop", "otherdrive": r"D:\Users\me\work", "unknown": None,
    }
    for sid, project in projects.items():
        _session(cfg.omp_sessions_dir, sid, project, "pathprobe")
    idx = Index(cfg)
    report = idx.run()
    assert not report.errors, report.errors
    try:
        all_ids = set(projects)
        for excluded, removed in [
            (("work",), {"base", "child", "elsewhere", "windows", "windowschild", "otherdrive"}),
            (("/home/me/work",), {"base", "child"}),
            (("work%", "work_"), {"percent", "percentchild", "underscore"}),
            ((r"C:\Users\me\work",), {"windows", "windowschild"}),
            (("C:/Users/me/work",), {"windows", "windowschild"}),
        ]:
            expected = all_ids - removed
            assert _ids(idx, exclude_projects=excluded) == expected
            result = find(idx, "pathprobe", scope=Scope(exclude_projects=excluded), limit=100)
            assert {group.session_id for group in result.groups} == expected
    finally:
        idx.close()
