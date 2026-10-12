# Competitor report: searching agent session history

Compiled 2026-10-07. Stars, licenses and dates come from the GitHub API on that day. Capabilities come from each project's README (and source code where noted). None of these tools were installed or run for this report, so treat capability rows as "documented", not "tested".

## Summary

- **The space is crowded.** More than 20 open tools search or browse coding-agent history. Three lead on breadth and adoption: **cass** (1,161 stars, Rust, 33 connectors), **Agent Sessions** (892 stars, macOS app) and **memex** (246 stars, Rust). SpecStory (1,347 stars) is adjacent: it captures sessions into Markdown rather than searching the agents' own stores.
- **omp and Hermes support is not unique to scry.** cass reads Oh My Pi (including sub-agent transcripts and profiles) and Hermes. memex reads Oh My Pi and Hermes, including Hermes profile databases. Agent Sessions and agent-session-search also read Hermes.
- **What scry does that nobody else documents as a core design:** output built for an agent's context window. Every view has a character budget, every result is a stable ref (`omp:01a07243/Scout1@12`), and every cut names the ref that recovers it. Views (`card`, `walk`, `tree`, `links`, `read`) zoom from one-line session summaries down to single turns, including `@N.k` sub-turns (skill invocations, bus mail). It also supports mirrored stores from other machines, redacts secrets at index and display time, and has no dependencies beyond Python's standard library.
- **Table stakes scry lacks:** an interactive picker with resume, Codex/Pi/Gemini/OpenCode adapters, one-command installers, an MCP server, semantic search and usage/cost reports.
- **License opening:** cass, the largest, uses "MIT with an OpenAI/Anthropic Rider" that withholds all rights from OpenAI, Anthropic and their affiliates. That is not an OSI-approved open-source license. A plainly licensed tool covering the same ground fills a real gap.

## Direct competitors: multi-agent search

| Tool | Language | Stars | License | Last push | Agents read | Interface | Notable |
|---|---|---|---|---|---|---|---|
| [cass](https://github.com/Dicklesworthstone/coding_agent_session_search) | Rust | 1,161 | MIT + OpenAI/Anthropic rider | 2026-10-07 | 33 connectors: Claude Code, Codex, Cursor, Gemini, Aider, Cline, OpenCode, Amp, Copilot, Pi, **Oh My Pi** (profiles, sub-agents, archive roots), **Hermes** (root and project-local `state.db`), Factory, Goose, Crush, OpenHands, more | TUI, `--robot`/JSON CLI | Hybrid BM25 + semantic + reranking, multi-machine remote sources, resume, HTML export, analytics. Custom SQLite and search engines. 3,600-line README |
| [Agent Sessions](https://github.com/jazzyalex/agent-sessions) | Swift | 892 | MIT | 2026-10-06 | Codex, Claude Code, OpenCode, Cursor Agent, Antigravity, **Hermes**, OpenClaw, Copilot CLI, more | macOS app | Browse, search, analyze and resume. macOS only (Homebrew cask) |
| [memex](https://github.com/nicosuave/memex) | Rust | 246 | MIT | 2026-10-07 | Claude Code, Codex, Cursor, OpenCode, Pi, **Oh My Pi**, OpenClaw, Copilot CLI, **Hermes** (root and profile directories) | TUI, CLI, MCP (stdio and HTTP), agent skill | BM25 plus optional local embeddings, resume, token-usage reconstruction, background daemon. Homebrew and script install |
| [recall (zippoxer)](https://github.com/zippoxer/recall) | Rust | 198 | MIT | 2026-01-14 | Claude Code, Codex, OpenCode, Factory | TUI (type to search, Enter to resume), JSON CLI | Tantivy search engine. **Indexes only message text; its parsers skip tool calls, tool results and thinking** (verified in source). Homebrew, WinGet, Cargo, AUR `recall` |
| [sessions](https://github.com/nicknisi/sessions) | TypeScript | 38 | MIT | 2026-09-15 | Claude Code, Codex, Pi | fzf picker, CLI, MCP | Resume, usage reports, agent memory import (from `pi-hermes` memory plugin and Claude memory, not Hermes sessions). Homebrew |
| [ai-sessions-mcp](https://github.com/yoavf/ai-sessions-mcp) | Go | 30 | MIT | 2026-08-17 | Claude Code, Gemini CLI, OpenCode, Codex | MCP server | Also uploads sessions to aisessions.dev |
| [chat-history](https://github.com/ay-bh/chat-history) | Rust | 12 | MIT | 2026-09-25 | Claude Code, Cursor, Codex | CLI, MCP | Search, inspect, export. Cargo |
| [recall (pratikgajjar)](https://github.com/pratikgajjar/recall) | Go | 7 | MIT | 2026-10-06 | Cursor, Claude Code, Codex, Pi, Cursor Agent CLI | CLI | Lua plugins for new sources, tags, cost stats, reopen in source tool. Homebrew, `go install`, script |
| [ah](https://github.com/nihen/ah) | Rust | 3 | MIT | 2026-09-28 | Claude Code, Codex, Gemini CLI, Copilot, Cursor | CLI | Search, inspect, resume. Homebrew, Cargo |
| [agent-session-search](https://github.com/benvenker/agent-session-search) | TypeScript | 2 | MIT | 2026-09-10 | Codex, Claude Code, Gemini, Cursor, Pi, **Hermes** (`~/.hermes/sessions`), Pool, plain-text roots | MCP server, CLI | Built on the FFF file finder. npm |
| [mnemo](https://github.com/marcelocantos/mnemo) | Go | 0 | Apache-2.0 | 2026-09-30 | Claude Code | MCP server | Persistent MCP memory over transcripts. Homebrew |
| [scritty](https://github.com/scritty-org/scritty) | — | 1 | none stated | 2026-09-03 | Claude Code, Codex, Copilot, Gemini and others it hosts | Terminal emulator with CLI and MCP | Captures conversations of the CLIs run inside it |

## Single-agent tools

| Tool | Agent | Language | Stars | License | Last push | What it does |
|---|---|---|---|---|---|---|
| [cchistory](https://github.com/eckardt/cchistory) | Claude Code | TypeScript | 137 | MIT | 2026-06-10 | "Like the shell `history` command, but for your Claude Code sessions" |
| [search-sessions](https://github.com/sinzin91/search-sessions) | Claude Code | Rust | 45 | MIT | 2026-06-27 | Fast search across all Claude Code sessions |
| [cxresume](https://github.com/lingtaolf/cxresume) | Codex | JavaScript | 15 | MIT | 2025-10-14 | Split-pane browser, search and resume with compressed context |
| [cc-sessions](https://github.com/FlorianBruniaux/cc-sessions) | Claude Code | Python | 13 | MIT | 2026-09-01 | Search, browse, analyze; incremental index |
| [cdxresume](https://github.com/sasazame/cdxresume) | Codex | TypeScript | 6 | MIT | 2026-08-01 | Terminal browser and resume for Codex sessions |
| [claude-transcript-exporter](https://github.com/GunitBindal/claude-transcript-exporter) | Claude Code | JavaScript | 6 | MIT | 2026-03-06 | Export transcripts with tool calls and sub-agent output |
| [resumer](https://github.com/jin-ttao/resumer) | Claude Code, Codex | Go | 2 | MIT | 2026-06-12 | One picker to resume sessions; single static binary |
| [codex-sessions](https://github.com/alexchexes/codex-sessions) | Codex | Python | 0 | MIT | 2026-07-16 | Search, rename, convert rollout files to Markdown/YAML |
| [codex-session-explorer](https://github.com/AugurCognito/codex-session-explorer) | Codex | TypeScript | 0 | MIT | 2026-07-06 | Read-only explorer over Codex's SQLite thread index |
| [claude-code-history](https://github.com/mike-grant/claude-code-history) | Claude Code | TypeScript | 0 | MIT | 2025-12-30 | Search Claude Code conversation history |

## Adjacent tools and built-in features

- **[SpecStory](https://github.com/specstoryai/getspecstory)** (Go, 1,347 stars, Apache-2.0, pushed 2026-10-07): captures sessions from Claude Code, Cursor CLI, Codex, Gemini CLI, Droid, Antigravity, Muse Code and the Cursor/VS Code extensions into local Markdown, with cloud sync and "Lore" skills. It saves sessions as they happen; it does not index the agents' own stores after the fact.
- **[yoru](https://www.yoru.sh/)**: audit-grade trails of Claude Code sessions via hooks, self-hosted.
- **Built into the agents:** Claude Code's `/resume` picker and transcript export ([docs](https://code.claude.com/docs/en/sessions)); Codex's local session search, available since v0.134 according to [this write-up](https://codex.danielvaughan.com/2026/06/01/codex-cli-session-history-local-search-rollout-format-knowledge-mining); Cursor's in-conversation search ([docs](https://cursor.com/help/ai-features/conversation-search)). Each covers only its own agent.

## Where scry stands

Rows for other tools reflect their READMEs (source code for recall's text-only indexing). "—" means not documented, not necessarily absent.

| Capability | scry | cass | memex | recall (zippoxer) |
|---|---|---|---|---|
| Oh My Pi sessions | Yes | Yes | Yes | No |
| Sub-agents as a tree (`tree`, `walk`) | Yes | Sub-agent transcripts indexed | — | No |
| Hermes, including profile databases | Yes | Root and project-local only | Yes | No |
| Claude Code, Cursor | Yes | Yes | Yes | Claude only |
| Codex, Pi, Gemini, OpenCode | No | Yes | Codex, Pi, OpenCode | Codex, OpenCode |
| Searches tool output | Yes, on request (`--speaker tool` or `any`; first 8,000 characters of each result) | — | — | No |
| Character-budgeted output, recoverable cuts, stable refs | Yes | — | — | No |
| Skill invocations and bus mail as sub-turns (`@N.k`) | Yes | — | — | No |
| Mirrored stores from other machines | Yes (config) | Yes (remote sources) | — | No |
| Secret redaction | Yes, at index and display | — | — | No |
| Interactive picker and resume | No | Yes | Yes | Yes |
| MCP server | No (omp custom tool only) | — | Yes | No |
| Semantic search | No | Yes | Optional | No |
| Usage and cost reports | No | Analytics | Yes | No |
| Install | git clone + `install.sh`, needs Python 3.11+ | Homebrew, Cargo, script | Homebrew, script | Homebrew, WinGet, Cargo, AUR |
| License | MIT | MIT + OpenAI/Anthropic rider | MIT | MIT |

## Openings

1. **Lead with agent-first reading.** Refs, budgets, recoverable cuts and zoom levels are scry's core design and nobody else markets them. Benchmark them against cass `--robot` and memex's MCP tools on real sessions before claiming the edge publicly.
2. **Stay plainly MIT or Apache-2.0.** It is the clearest difference from cass, and the rider restricts users who run agents on Anthropic or OpenAI models.
3. **Close the table stakes before launch:** interactive picker with resume, Codex and Pi adapters first (every serious competitor has them), then Gemini and OpenCode, plus single-binary releases (Homebrew, AUR, script) and an MCP server.
4. **Don't race on connector count.** cass has 33 connectors. Compete on depth for the agents you use daily (omp sub-agents, Hermes profiles, sub-turns) and on reading quality.
5. **Run the comparison for real.** Install cass and memex in isolation, index the same sessions, and compare answers to the same questions. That turns this report's "documented" rows into "tested" ones.

## Name check: `scry`

- Unused by any official Arch package, the AUR or Homebrew (checked 2026-10-07 with a freshly synced `pacman -F` database, the AUR RPC and the Homebrew API).
- crates.io `scry` is an unrelated Scryfall card-search tool; a Rust crate would need another name (e.g. `scry-cli`). Go modules have no registry clash.
- No agent-history tool by that name turned up on the web or GitHub.
- Names rejected for clashes with same-purpose or adjacent tools: `recall` (two agent-history tools), `yore` (agent context engine), `retrace` (agent trace CLI), `trawl` (Claude MCP tool), `scrollback` (agent-session tool on PyPI), `seance` (AI agent multiplexer on the AUR), `agentlog`, `annals`, `sesh`, `lore`.
