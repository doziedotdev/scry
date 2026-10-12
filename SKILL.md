---
name: scry
description: Find and walk past or live agent sessions across every harness (Claude Code, Cursor, OMP, Hermes) by ref with the `scry` CLI. Use for "did we work on X", "where did that happen", "what did the user ask", "how did that session end", "what is that other session doing", and to hand context to another agent as a ref instead of pasting transcripts.
---

# scry

The past is an address space. Search returns refs; every other view takes one.

```
ref := [store:]session[/agent...][@N | @N.k | @N-M | @N- | @-N]
omp:01a07243   omp:01a07243/Scout1@12   hermes.work:20260823_071850   omp:01a07243@-5
```

Any unique id prefix works, the store can be omitted when unambiguous, and a ref is stable across index rebuilds and machines. `@N.k` is the kth sub-turn after turn N: a skill invocation or a bus mail the harness stored outside its message stream. A bare `@N` reads the integer turn only; ranges include the sub-turns inside them.

## Zoom levels, cheapest first

| Ask | Get |
|---|---|
| nothing | overview: stores, projects, latest sessions, the grammar |
| words | find: sessions with pointers; best, first and last hit per session; the tier used (one turn, one session, any word) |
| a session or agent ref | card: title, summary, how it started, how it ended, tools, agents, files written, recent activity |
| `ref@N`, `@N.k`, `@N-M`, `@-N` | read: those turns, capped per turn, each cut naming the ref that recovers it |
| `view=walk` | one line per turn, tool runs folded; `speaker=user` is the story of what was asked |
| `view=tree` / `view=links` | the agent tree; parent, children, artifacts, same-project sessions elsewhere, sessions mentioning this one |
| `view=sessions` / `projects` / `live` | listings; live shows what other sessions are doing right now |
| `view=skills` | skill usage per skill: explicit invocations, skill file reads, sessions, main or agent threads, a ref to the latest use |
| `view=export` | the whole thread to a file; read it in ranges |

## Rules of the road

- Start with words or a ref; copy refs from the output; follow the `next:` line.
- Output is capped by `budget` (default 4000 chars). Raise it only when a `more:` or `…` line says so; otherwise narrow with `since`, `project`, `store`, `speaker`.
- `find` searches user, assistant, summary and mail text. For file paths, commands and function names use `speaker=tool`; for reasoning `speaker=thinking`; for everything `speaker=any`.
- All words must be in one turn; the tool falls back to one session, then any word, and says which. Zero hits prints per-term counts so you know which word to drop.
- Handing work to another agent: give the ref (`omp:01a07243@186`), not the text.
- The index catches up on every call; live sessions are searchable within seconds.

CLI: `scry WORDS...`, `scry REF`, `scry walk|tree|links|export REF`, `scry sessions|projects|live|skills`, `--json` for the `{schema, view, text, refs, meta}` envelope. Config: `~/.config/scry/config.toml`. Index: `~/.cache/scry/` (derived; safe to delete).
