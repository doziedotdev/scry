# scry

One lens over every past and present agent session on a machine: Claude Code, Cursor, Oh My Pi (OMP), Hermes and Google Antigravity (`agy`) today, any harness with an adapter tomorrow. Stdlib-only Python 3.11+, one SQLite index, no network, no telemetry.

The tool exists so that an agent (any model, any harness, in a subagent, in a parallel session, or years later) can answer "did we work on X, where, what was asked, how did it end, what did that subagent do" without pasting transcripts into its context. It does that with three ideas:

1. **Everything has an address.** Search returns refs; every other view takes one.
2. **Zoom levels.** Find, card, walk, read, export: each costs more than the last, and you only pay for what you ask.
3. **Budgets.** Output never exceeds its character budget, and every cut names the ref that recovers it.

## Refs

```
ref := [store:]session[/agent...][@turns]
turns := N | N.k | N-M | N- | -N      (-N is the last N turns)

omp:01a07243                       a session (any unique id prefix works; the store may be omitted when unambiguous)
omp:01a07243/Scout1                a child agent, named as the harness named it
omp:01a07243/__advisor.learn@7     turn 7 of that agent
omp:01a07243@180-186               turns 180 to 186 of the session's main thread
omp:01a07243@2480.1                the first sub-turn after turn 2480 (a skill invocation, a bus mail)
omp:01a07243@-5                    the last five turns
hermes.work:20260823_071850        a Hermes session in the "work" profile bank
```

Refs derive from the transcripts, never from index row ids. Rebuilding the index, or indexing the same synced transcripts on another machine, yields identical refs, so two agents can exchange them. Turn numbers count messages as the harness stores them; thinking, text and tool calls of one assistant message are parts of one turn, and a tool result is its own `tool` turn.

Entries a harness stores outside its message stream become sub-turns `N.k`: the kth such entry after integer turn `N` (`@0.k` when it precedes the first turn). OMP skill invocations (the user's invocation words, never the skill body) and bus mail are sub-turns today. A bare `@N` means the integer turn only; ranges include the sub-turns inside them, and spans starting at `@1` also show `@0.k` entries. Sub-turns never renumber integer turns.

## Using it

```console
scry                                   overview: stores, projects, latest sessions, the grammar
scry flaky test retry                 find: sessions with pointers
scry omp:01a07243                      card: how it started, how it ended, agents, tools, files
scry omp:01a07243@-3                   read: the last three turns
scry walk omp:01a07243 --speaker user  one line per turn; with speaker=user, the story of what was asked
scry tree omp:01a07243                 the agent tree with spawn and result turns
scry links omp:01a07243                parent, children, artifacts, same-project sessions in other stores, sessions that mention this one
scry sessions --project myproject --since 7d
scry projects
scry live                              sessions active in the last 15 minutes and what they are doing
scry skills --since 7d                 skill usage: invocations and skill reads per skill, by thread kind, with a ref to the latest use
scry export omp:01a07243               whole thread to a file; never inline
```

Options: `--store omp,hermes.work` (default: every store not excluded in config), `--project`, `--speaker user,assistant,tool,summary,system,thinking,mail,any`, `--since 24h|7d|2w|3m|1y|YYYY-MM-DD`, `--until`, `--limit`, `--from`, `--full`, `--budget N` (default 4000, max 60000), `--json` (envelope `{schema, view, text, refs, meta}`), `--no-index`, `--rebuild`.

Search: all words must appear in one turn. If nothing matches, the tool retries for all words within one session, then any word, and the header names the tier it used. Quoted phrases, `-word` exclusion and `word*` prefixes work. `find` searches user, assistant, summary and mail text by default; `--speaker tool` searches tool output and tool-call arguments (file paths, commands, function names), `--speaker thinking` searches reasoning.

Skills: `uses` = `invoked` + `read`. An invocation is an OMP skill sub-turn (`[skill <name>]`) or a Claude Code `Skill` call. A read is a file read of `skill://<name>` (or a file under it) or of `.../<name>/SKILL.md` (OMP `read`, Hermes/Antigravity `read_file`, Claude Code `Read`), or a Hermes `skill_view` call. Each use is timed by its turn, so `--since` and `--until` bound the uses. `--json` puts one entry per skill in `meta.skills` (`name uses invoked read sessions threads first_ts last_ts last_ref`, `threads` split into main, subagent, advisor and sidechain), sorted by uses then name.

The index catches up before every answer (about 50 ms when nothing changed), so live sessions are searchable. Pass `--no-index` to skip that, `--view index` to see the report, `--rebuild` to purge and rebuild.

## Output rules

- Every view starts with a header naming what was asked, what exists and what is shown, and ends with a `next:` line of concrete calls.
- Every elision names its recovery: `… [+2,314 chars: ref=omp:01a07243@17 full=true]`, `more: ref=omp:01a07243@41- view=walk`.
- Every turn carries a one-line gist computed at index time: first line for people, the harness's `intent` (or a synthesized `read core/urls.py`) for tool calls, `ok 2.1k` or `error` for results. `walk` is the list of gists, with runs of tool turns folded, which is why a 500 turn session walks in 40 lines.
- Errors teach: an unknown prefix lists the nearest ids, an unknown agent lists the agents, zero hits prints per-term counts.

## Configuration

`${XDG_CONFIG_HOME:-~/.config}/scry/config.toml`:

```toml
[defaults]
budget = 4000
# exclude_stores = ["hermes.work"]     # indexed, searched only when named with --store
# default_stores = ["omp", "claude"]   # allowlist form

[stores."omp.mirror"]                  # a synced copy of another machine's transcripts; refs stay identical
harness = "omp"
location = "~/mirrors/other/omp-sessions"
# metadata = "..."                     # optional side database (Cursor keeps titles in state.vscdb)
```

A mirrored Hermes home (`harness = "hermes"`, location a directory) expands to one store per bank. The usual layout is one install per machine, each indexing its own harnesses; transcripts stay where they were produced. Everything found on the machine is searched by default. `REAL_HOME` takes precedence over `HOME`; `CLAUDE_PROJECTS_DIR` overrides the Claude location.

The index lives at `~/.cache/scry/index.sqlite3` (mode 0600, directory 0700) and exports under `~/.cache/scry/exports/`. Both are derived, rebuildable data. Build workers default to all but one core (at most 16); set `SCRY_JOBS` to override.

## Redaction: best-effort, not a security boundary

Credential shapes (secret-looking assignments and JSON/YAML keys, `Bearer` tokens, common provider token prefixes, PEM private keys, secret query parameters) are replaced before indexing and again on display. Treat the index and every transcript as sensitive anyway. Originals are never modified.

## Layout

```
scry/
  model.py        the normalized model and the ref grammar
  adapters/       one module per harness; base.py is the contract (see docs/corpora.md)
  index.py        one incremental SQLite index with FTS5, checkpoints, memo, and ref resolution
  query.py        find: tiers, scoring, grouping by session
  views.py        overview, find, card, walk, read, tree, links, sessions, projects, live, skills, export
  budget.py       the output ceiling and its helpers
  gist.py         one line per turn, per call, per task
  redact.py
  cli.py
```

Adding a harness is one adapter module plus a line in `adapters/__init__.py`; `tests/adapter_contract.py` checks it.

## Install

Runtime requires only Python 3.11+ with SQLite FTS5. No third-party Python packages are needed.

The installer generates `~/.local/bin/scry` pointing at this checkout, so keep the checkout in place. Make sure `~/.local/bin` is on your `PATH` before running `scry`:

```console
git clone https://github.com/doziedotdev/scry.git
cd scry
./install.sh
export PATH="$HOME/.local/bin:$PATH"
scry  # first run builds the index; minutes for a large machine, incremental afterwards
```

Add the `export PATH` line to your shell's startup file to keep it for new shells. Alternatively, set `BIN_DIR` to a directory already on your `PATH` when installing: `BIN_DIR="$HOME/bin" ./install.sh`.

## Development

Run these commands from the checkout. `pytest` is a development-only dependency, not a runtime requirement. A virtual environment keeps it separate from your system Python:

```console
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install pytest
python3 -m pytest tests -q
```
