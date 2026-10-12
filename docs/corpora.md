# Harness adapters

Every harness is mapped onto the same model (`scry/model.py`): a store holds sessions; a session is a tree of threads (the main thread plus child agents); a thread is a contiguous sequence of turns; a turn has parts (text, thinking, tool calls, or a tool result). The contract adapters implement is `scry/adapters/base.py`; `tests/adapter_contract.py` checks any adapter against it.

Rules every adapter keeps: never modify a transcript; open databases read-only; ordinals are 1-based, contiguous, and depend only on source content; one turn per stored message; a `user` turn is a human (synthetic, injected or agent-attributed user rows are `system`); harness compaction and summary rows are `summary`; raw text out, the core redacts. Entries worth indexing that live outside the message stream become sub-turns (`Turn.sub` 1, 2, ... after the last integer ordinal, 0 when none precede them): they never renumber integer turns, and the checkpoint's `sub` count lets a resume continue their numbering.

| | Session | Child agents | Spawn and result links | Summaries | Artifacts | Timestamps |
|---|---|---|---|---|---|---|
| **OMP** | `~/.omp/agent/sessions/<slug>/<stamp>_<id>.jsonl`; id, cwd and title from `session`/`title` rows; `native_handle` is `history://<id>` | files in the sibling directory `<stamp>_<id>/`: `Name.jsonl` is a named subagent (kind `subagent`), `__advisor.role.jsonl` an advisor; `session_init` gives agent type, task, model; nested directories nest paths | the parent's `task` tool call whose `tasks[].name` matches, and the tool result that delivered it | `compaction` rows | `Name.md` / `Name.json` outputs, `NNN.tool.log` spilled tool output; tombstones ignored | per message |
| **Claude Code** | `~/.claude/projects/<encoded cwd>/<id>.jsonl`; project from `cwd`, else the directory name decoded against the filesystem (`project_slug` kept); branch in `extra` | `isSidechain` rows grouped by their root uuid (kind `sidechain`, logical location `file#sidechain=<uuid>`), and `<id>/subagents/**/agent-*.jsonl` files (kind `subagent`) | the `Task`/`Agent` tool_use whose prompt equals the child's first user text, and its tool_result | `summary` rows | none | per row |
| **Hermes** | one bank per profile: `~/.hermes/state.db` is store `hermes`, `~/.hermes/profiles/<p>/state.db` is `hermes.<p>`; sessions with `parent_session_id` NULL | sessions whose `parent_session_id` points at the root, labelled from `async_delegations` when resolvable, else their id; grandchildren nest | delegation rows | `compacted` messages | none | per message; resume by message id |
| **Cursor** | `~/.cursor/projects/<encoded cwd>/agent-transcripts/<composerId>/<composerId>.jsonl`; title, created/updated and `isSubagent` from `composerHeaders` in `~/.config/Cursor/User/globalStorage/state.vscdb` (keyed reads only; optional) | `<composerId>/subagents/<id>.jsonl` (path is the stable child id, label from metadata) | the `Task` tool_use naming the child, best effort | none observed | none | only inside `<timestamp>` wrappers on user text; carried forward to assistant turns; file mtime as last resort |

Cursor transcripts hold no tool results and no compaction rows; `turn_ended` marker rows are not turns.

OMP `custom_message` rows with `display: true` become sub-turns when their `customType` is `skill-prompt` (speaker `user`, text `[skill <name>] <args>`, falling back to the text after the last `\nUser: ` when `details.args` is absent) or `bus:mail` (speaker `mail`, one `from <seat> (<relation>, <mode>, omp:<session>): <body>` line per mail). `bus:card`, `irc:incoming` and every other custom row stay unindexed.

## Incrementality

The core keeps a checkpoint per thread source: fingerprint (`size:mtime_ns` for files, `maxid:N` for databases), byte offset after the last complete record, last integer ordinal, last native id, a guard hash of the bytes before the offset, and the sub-turn count after that ordinal. An unchanged fingerprint skips the thread; a shorter file or a guard mismatch (in-place replacement) resets it; otherwise the adapter resumes at the offset (files) or after the native id (databases). Incomplete trailing lines wait for the next pass.

Discovery must be cheap on unchanged sources. Adapters that must parse a file to learn its structure (Claude sidechains, OMP session facts) keep the result in the core's `memo` table keyed by fingerprint.

## Adding a harness

1. Write `scry/adapters/<name>.py` with a class exposing `harness`, `stores(cfg)`, `discover(store)`, `session_meta(source)`, `read_thread(source, thread, checkpoint)`.
2. Register it in `scry/adapters/__init__.py`.
3. Add synthetic fixtures under `tests/fixtures/<name>/` and a test that calls `tests/adapter_contract.py::check_store`.
