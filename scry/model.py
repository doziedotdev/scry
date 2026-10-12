"""The normalized model every harness is mapped onto.

Three nouns, one address grammar:

    store    one transcript source: a harness plus optional partition   omp, claude, hermes.work
    session  one root conversation in a store                           omp:01a07243
    agent    a child conversation inside a session (subagent, advisor,
             sidechain), named as the harness named it                  omp:01a07243/Scout1
    turn     one message, numbered from 1 inside its session or agent   omp:01a07243@186

    ref := [store ":"] session ["/" agent ("/" agent)*] ["@" turns]
    turns := N | N-M | N- | -N        (-N means the last N turns)

Refs derive from the source transcripts, never from index row ids, so a rebuilt
index, or another machine indexing the same synced transcripts, yields identical
refs. Any unique prefix of a session id resolves, like a git SHA.

Inside a turn there are parts (text, thinking, tool calls, tool result). Parts are
never separate turns: turn numbers match what a person reading the harness sees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SPEAKERS = ("user", "assistant", "tool", "system", "summary", "mail")
THREAD_KINDS = ("main", "subagent", "advisor", "sidechain")

# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------

_STORE_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)*$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_TURNS_RE = re.compile(r"^(?:(\d+)(?:\.(\d+)|-(\d+)?)?|-(\d+))$")


@dataclass(frozen=True, slots=True)
class TurnRange:
    """Inclusive 1-based turn selection. ``last`` selects the final N turns.

    ``sub`` selects one sub-turn ``N.k``: the kth entry after integer turn N that
    the harness stored outside its message stream (an OMP skill invocation, a bus
    mail). Ranges stay integer-addressed and include the sub-turns inside them;
    a bare ``@N`` means the integer turn only.
    """

    start: int | None = None
    end: int | None = None
    last: int | None = None
    sub: int | None = None

    def __str__(self) -> str:
        if self.last is not None:
            return f"-{self.last}"
        if self.sub is not None:
            return f"{self.start}.{self.sub}"
        if self.start is not None and self.end is not None:
            return f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        if self.start is not None:
            return f"{self.start}-"
        return ""

    def resolve(self, turn_count: int) -> tuple[int, int]:
        """Clamp to ``[1, turn_count]`` and return an inclusive ``(first, last)``.

        A sub-turn selection resolves to its integer turn; the caller reads the
        one ``(N, k)`` row instead of the range.
        """
        if turn_count <= 0:
            return (1, 0)
        if self.last is not None:
            return (max(1, turn_count - self.last + 1), turn_count)
        start = max(1, self.start or 1)
        end = min(turn_count, self.end if self.end is not None else turn_count)
        return (start, max(start - 1, end))

    @property
    def single(self) -> bool:
        """A bare ``@N`` (or the same address written ``@N-N``): the integer turn only."""
        return self.sub is None and self.last is None and self.start is not None and self.start == self.end


@dataclass(frozen=True, slots=True)
class Ref:
    store: str | None
    session: str
    agent: tuple[str, ...] = ()
    turns: TurnRange | None = None

    @property
    def path(self) -> str:
        return "/".join(self.agent)

    def with_store(self, store: str, session: str | None = None) -> "Ref":
        return Ref(store, session or self.session, self.agent, self.turns)

    def thread(self) -> "Ref":
        """Drop the turn selection."""
        return Ref(self.store, self.session, self.agent, None)

    def root(self) -> "Ref":
        """Drop agents and turns: the session itself."""
        return Ref(self.store, self.session, (), None)

    def parent(self) -> "Ref | None":
        if self.turns is not None:
            return self.thread()
        if self.agent:
            return Ref(self.store, self.session, self.agent[:-1], None)
        return None

    def at(self, start: int, end: int | None = None, *, sub: int | None = None) -> "Ref":
        return Ref(self.store, self.session, self.agent, TurnRange(start, end if end is not None else start, sub=sub))

    def __str__(self) -> str:
        out = f"{self.store}:{self.session}" if self.store else self.session
        if self.agent:
            out += "/" + "/".join(self.agent)
        if self.turns is not None:
            out += f"@{self.turns}"
        return out


class RefError(ValueError):
    """A string that is not a ref, with a message that names the grammar."""


GRAMMAR = "ref := [store:]session[/agent...][@N | @N.k | @N-M | @N- | @-N]   e.g. omp:01a07243/Scout1@12"


def parse_ref(text: str) -> Ref:
    raw = text.strip()
    if not raw:
        raise RefError(f"empty ref. {GRAMMAR}")
    body, _, turns_text = raw.partition("@")
    turns: TurnRange | None = None
    if _ != "":
        match = _TURNS_RE.match(turns_text.strip())
        if not match:
            raise RefError(f"bad turn selection {turns_text!r} in {raw!r}. {GRAMMAR}")
        start, sub, end, last = match.groups()
        if last is not None:
            turns = TurnRange(last=int(last))
        elif sub is not None:
            turns = TurnRange(int(start), int(start), sub=int(sub))
        elif end is not None:
            turns = TurnRange(int(start), int(end))
        elif turns_text.strip().endswith("-"):
            turns = TurnRange(int(start), None)
        else:
            turns = TurnRange(int(start), int(start))
        if turns.sub is not None and turns.sub < 1:
            raise RefError(f"sub-turn numbers start at 1: {raw!r}")
        floor = 0 if turns.sub is not None else 1
        if turns.start is not None and turns.start < floor or turns.last is not None and turns.last < 1:
            raise RefError(f"turn numbers start at 1: {raw!r}")
        if turns.start is not None and turns.end is not None and turns.end < turns.start:
            raise RefError(f"turn range runs backwards: {raw!r}")
    store: str | None = None
    if ":" in body:
        store, _, body = body.partition(":")
        store = store.strip()
        if not _STORE_RE.match(store):
            raise RefError(f"bad store {store!r} in {raw!r}. {GRAMMAR}")
    segments = [seg for seg in body.strip().split("/")]
    if not segments or not segments[0]:
        raise RefError(f"missing session in {raw!r}. {GRAMMAR}")
    session = segments[0]
    if not _SESSION_RE.match(session):
        raise RefError(f"bad session id {session!r} in {raw!r}. {GRAMMAR}")
    agents = tuple(seg for seg in segments[1:])
    if any(not seg for seg in agents):
        raise RefError(f"empty agent segment in {raw!r}. {GRAMMAR}")
    return Ref(store, session, agents, turns)


def looks_like_ref(text: str) -> bool:
    """True when a query string is really an address (store prefix, @turns, or a bare id)."""
    raw = text.strip()
    if not raw or " " in raw:
        return False
    if "@" in raw or ":" in raw:
        try:
            parse_ref(raw)
            return True
        except RefError:
            return False
    # A bare token counts as a ref only when it looks like an id, not a word.
    return bool(re.match(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9_-]{5,}$", raw))


# ---------------------------------------------------------------------------
# Turns and threads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    """One tool invocation inside an assistant turn."""

    name: str
    args: str = ""  # compact JSON or raw text of the arguments, redacted at index time
    intent: str = ""  # the harness's one-line description of the call, when it has one
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One message. ``ordinal`` is 1-based inside its thread and must be contiguous.

    ``sub`` is 0 for an ordinary turn and 1, 2, ... for sub-turns: entries the
    harness stored outside its message stream, numbered after the last integer
    turn before them (0 when none). Integer ordinals never move for sub-turns.
    """

    ordinal: int
    speaker: str  # one of SPEAKERS
    text: str = ""  # visible text; for tool turns, the tool's output
    thinking: str = ""  # assistant reasoning, or ""
    calls: tuple[Call, ...] = ()
    ts: float | None = None  # unix seconds, None when the harness records none
    tool: str | None = None  # tool name for tool-result turns
    call_id: str | None = None  # links a tool-result turn to its call
    is_error: bool = False
    native_id: str | None = None  # the harness's own id for this message
    offset: int | None = None  # byte offset of the record in a JSONL source, for re-reads
    sub: int = 0  # 0 for an integer turn, else the sub-turn index after ``ordinal``

    def __post_init__(self) -> None:
        if self.speaker not in SPEAKERS:
            raise ValueError(f"speaker {self.speaker!r} not in {SPEAKERS}")
        if self.ordinal < 1 and not (self.ordinal == 0 and self.sub >= 1):
            raise ValueError("ordinals start at 1")
        if self.sub < 0:
            raise ValueError("sub-turns start at 0")

    @property
    def label(self) -> str:
        """The turn number as refs print it: ``N`` or ``N.k``."""
        return f"{self.ordinal}.{self.sub}" if self.sub else str(self.ordinal)


@dataclass(frozen=True, slots=True)
class Artifact:
    """A file the session produced beside its transcript (subagent output, spilled tool log)."""

    name: str
    path: str
    kind: str  # output | log | other
    size: int
    owner: str = ""  # agent path that produced it, "" for the session itself


@dataclass(frozen=True, slots=True)
class ThreadMeta:
    """One conversation stream: the session's main thread or a child agent."""

    path: tuple[str, ...]  # () for main; ("Scout1",) ; ("Scout1", "Sub")
    kind: str  # one of THREAD_KINDS
    label: str = ""  # display name; the harness's own name for the agent
    agent_type: str = ""  # scout, task, reviewer, ... when known
    task: str = ""  # the delegated task, first line, when known
    model: str = ""
    started: float | None = None
    ended: float | None = None
    status: str = ""  # completed | failed | aborted | "" when unknown
    native_id: str | None = None
    source: str = ""  # file path or db uri this thread is read from
    spawn_ordinal: int | None = None  # parent turn that launched it, when known
    result_ordinal: int | None = None  # parent turn that received its result, when known

    def __post_init__(self) -> None:
        if self.kind not in THREAD_KINDS:
            raise ValueError(f"kind {self.kind!r} not in {THREAD_KINDS}")
        if (self.kind == "main") != (self.path == ()):
            raise ValueError("main thread has the empty path and only the main thread does")


@dataclass(frozen=True, slots=True)
class SessionMeta:
    store: str
    session_id: str
    project: str | None = None  # absolute working directory when the harness records one
    project_slug: str | None = None  # the harness's encoded project name, when lossy decoding was needed
    title: str = ""
    started: float | None = None
    ended: float | None = None
    status: str = ""  # ended | live | "" unknown; adapters may leave "" and let the index infer from activity
    native_handle: str = ""  # e.g. history://<id> in OMP, so an agent inside that harness can use its own reader
    source: str = ""  # root transcript path
    model: str = ""
    artifacts: tuple[Artifact, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)  # small harness facts worth showing (branch, end reason)


@dataclass(frozen=True, slots=True)
class StoreSpec:
    """A physical transcript source. ``name`` is the ref prefix (``omp``, ``hermes.work``)."""

    name: str
    harness: str  # omp | claude | hermes | cursor | ...
    location: str  # directory or database path
    exists: bool = True
    metadata: str = ""  # optional side database the harness keeps titles in (Cursor: state.vscdb or a trimmed copy)
