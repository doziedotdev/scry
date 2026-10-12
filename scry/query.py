"""Find: terms in, sessions with pointers out.

Tiers, tried in order and named in the header so the reader knows how strict the
match was:

    turn      every term in one turn                 (default)
    session   every term somewhere in one session
    any       any term anywhere

Speakers decide which columns are searched: user/assistant/summary/system match the
visible text, tool matches tool output and tool-call arguments, thinking matches
reasoning, any matches everything.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from .index import Index
from .model import Ref

DEFAULT_SPEAKERS = ("user", "assistant", "summary", "mail")
ALL_SPEAKERS = ("user", "assistant", "tool", "summary", "system", "thinking", "mail")
CANDIDATES = 600  # FTS rows fetched before rescoring; enough for good grouping, small enough to stay fast
FTS_SPECIAL = re.compile(r'["*()^{}:]')


@dataclass(frozen=True, slots=True)
class Term:
    text: str
    phrase: bool = False
    prefix: bool = False
    negate: bool = False

    def fts(self) -> str:
        body = FTS_SPECIAL.sub(" ", self.text).strip()
        body = f'"{body}"'
        if self.prefix:
            body += "*"
        return body

    def matches(self, haystack: str) -> bool:
        return self.text.lower() in haystack


def parse_terms(q: str | list[str]) -> list[Term]:
    """Words, "quoted phrases", -excluded, and trailing * prefixes."""
    text = " ".join(q) if isinstance(q, list) else q
    terms: list[Term] = []
    for match in re.finditer(r'(-)?"([^"]+)"|(-)?(\S+)', text):
        neg1, phrase, neg2, word = match.groups()
        if phrase is not None:
            body = phrase.strip()
            if body:
                terms.append(Term(body, phrase=True, negate=bool(neg1)))
            continue
        body = word.strip()
        if body in {"-", "*"} or not body:
            continue
        prefix = body.endswith("*") and len(body) > 1
        body = body.rstrip("*")
        if body.lower() in {"and", "or", "not"}:
            continue
        if body:
            terms.append(Term(body, prefix=prefix, negate=bool(neg2)))
    return terms


@dataclass(slots=True)
class Hit:
    turn_pk: int
    session_pk: int
    ref: Ref
    store: str
    session_id: str
    thread_path: str
    ordinal: int
    sub: int
    speaker: str
    ts: float | None
    snippet: str
    gist: str
    lexical: float  # -bm25, larger is better
    coverage: float = 0.0
    score: float = 0.0

@dataclass(slots=True)
class SessionGroup:
    session_pk: int
    store: str
    session_id: str
    hits: list[Hit]
    score: float
    covered: int
    total: int = 0

    @property
    def best(self) -> Hit:
        return self.hits[0]


@dataclass(slots=True)
class FindResult:
    terms: list[Term]
    tier: str
    groups: list[SessionGroup]
    total_hits: int
    per_term: dict[str, int] = field(default_factory=dict)
    scope_note: str = ""


@dataclass(frozen=True, slots=True)
class Scope:
    stores: tuple[str, ...] | None = None
    project: str | None = None
    since: float | None = None
    until: float | None = None
    session_pk: int | None = None  # find inside one session
    thread_pk: int | None = None  # find inside one agent


def find(index: Index, q: str | list[str], *, speakers: tuple[str, ...] = DEFAULT_SPEAKERS, scope: Scope = Scope(), limit: int = 8, hits_per_session: int = 3) -> FindResult:
    terms = parse_terms(q)
    positive = [t for t in terms if not t.negate]
    if not positive:
        return FindResult(terms, "none", [], 0)
    hits = _search(index, terms, speakers, scope, conjunctive=True)
    tier = "turn"
    if not hits and len(positive) > 1:
        hits = _search(index, terms, speakers, scope, conjunctive=False)
        groups = _group(hits, positive)
        if any(g.covered == len(positive) for g in groups):
            tier = "session"
            hits = [h for g in groups if g.covered == len(positive) for h in g.hits]
        else:
            tier = "any"
    groups = _group(hits, positive)
    groups.sort(key=lambda g: (-g.score, -(g.hits[0].ts or 0)))
    for g in groups:
        g.hits = _pick(g.hits, hits_per_session)
    per_term = {}
    if not hits or tier != "turn":
        per_term = {t.text: _count(index, t, speakers) for t in positive}
    return FindResult(terms, tier, groups[:limit], len(hits), per_term)


def _search(index: Index, terms: list[Term], speakers: tuple[str, ...], scope: Scope, *, conjunctive: bool) -> list[Hit]:
    positive = [t.fts() for t in terms if not t.negate]
    negative = [t.fts() for t in terms if t.negate]
    joined = (" AND " if conjunctive else " OR ").join(positive)
    if len(positive) > 1:
        joined = f"({joined})"
    for neg in negative:
        joined = f"{joined} NOT {neg}"
    groups = _column_groups(speakers)
    seen: dict[int, Hit] = {}
    now = time.time()
    for columns, speaker_filter in groups:
        expr = f"{{{' '.join(columns)}}}: {joined}" if columns else joined
        sql = (
            "SELECT t.id AS turn_pk, t.ordinal, t.sub, t.speaker, t.ts, t.gist, th.path, th.id AS thread_pk, se.id AS session_pk, se.store, se.session_id, "
            "bm25(turn_fts, 1.0, 0.5, 0.7) AS rank, snippet(turn_fts, -1, '«', '»', '…', 16) AS snip "
            "FROM turn_fts JOIN turn t ON t.id = turn_fts.rowid JOIN thread th ON th.id = t.thread_pk JOIN session se ON se.id = th.session_pk "
            "WHERE turn_fts MATCH ?"
        )
        params: list = [expr]
        if speaker_filter:
            sql += " AND t.speaker IN (%s)" % ",".join("?" * len(speaker_filter))
            params.extend(speaker_filter)
        if scope.stores:
            sql += " AND se.store IN (%s)" % ",".join("?" * len(scope.stores))
            params.extend(scope.stores)
        if scope.project:
            sql += " AND (se.project LIKE ? OR se.project_slug LIKE ? OR se.project LIKE ?)"
            params.extend([scope.project + "%", "%" + scope.project + "%", "%/" + scope.project.strip("/") + "%"])
        if scope.since is not None:
            sql += " AND COALESCE(t.ts, se.activity) >= ?"
            params.append(scope.since)
        if scope.until is not None:
            sql += " AND COALESCE(t.ts, se.started) <= ?"
            params.append(scope.until)
        if scope.session_pk is not None:
            sql += " AND se.id = ?"
            params.append(scope.session_pk)
        if scope.thread_pk is not None:
            sql += " AND th.id = ?"
            params.append(scope.thread_pk)
        sql += " ORDER BY rank LIMIT ?"
        params.append(CANDIDATES)
        for row in index.db.execute(sql, params):
            if row["turn_pk"] in seen:
                continue
            path = tuple(row["path"].split("/")) if row["path"] else ()
            seen[row["turn_pk"]] = Hit(
                turn_pk=row["turn_pk"], session_pk=row["session_pk"],
                ref=Ref(row["store"], row["session_id"], path).at(row["ordinal"], sub=row["sub"] or None),
                store=row["store"], session_id=row["session_id"], thread_path=row["path"], ordinal=row["ordinal"], sub=row["sub"] or 0,
                speaker=row["speaker"], ts=row["ts"], snippet=_clean_snippet(row["snip"]), gist=row["gist"], lexical=-float(row["rank"]),
            )
    hits = list(seen.values())
    if not hits:
        return hits
    positive_terms = [t for t in terms if not t.negate]
    max_lex = max(h.lexical for h in hits) or 1.0
    for h in hits:
        hay = (h.snippet + " " + h.gist).lower()
        h.coverage = sum(1 for t in positive_terms if t.matches(hay)) / max(1, len(positive_terms))
        age_days = ((now - h.ts) / 86400.0) if h.ts else 365.0
        recency = 0.5 ** (age_days / 30.0)
        h.score = 0.6 * (h.lexical / max_lex) + 0.25 * h.coverage + 0.15 * recency
    return hits


def _column_groups(speakers: tuple[str, ...]) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Which FTS columns and which speaker rows each requested speaker maps to."""
    if "any" in speakers or set(speakers) >= set(ALL_SPEAKERS):
        return [((), ())]
    groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    text_speakers = tuple(s for s in speakers if s in ("user", "assistant", "summary", "system", "tool", "mail"))
    if text_speakers:
        groups.append((("search_text",), text_speakers))
    if "tool" in speakers:
        groups.append((("calls",), ("assistant",)))
    if "thinking" in speakers:
        groups.append((("thinking",), ("assistant",)))
    return groups


def _group(hits: list[Hit], positive: list[Term]) -> list[SessionGroup]:
    by_session: dict[int, list[Hit]] = {}
    for h in hits:
        by_session.setdefault(h.session_pk, []).append(h)
    groups = []
    for session_pk, items in by_session.items():
        items.sort(key=lambda h: -h.score)
        hay = " ".join((h.snippet + " " + h.gist).lower() for h in items)
        covered = sum(1 for t in positive if t.matches(hay))
        score = items[0].score + 0.04 * math.log2(1 + len(items)) + 0.05 * (covered / max(1, len(positive)))
        groups.append(SessionGroup(session_pk, items[0].store, items[0].session_id, items, score, covered, len(items)))
    return groups


def _pick(hits: list[Hit], n: int) -> list[Hit]:
    """Best hit, then the earliest and latest by ordinal on the main thread, so a session reads as a span."""
    if len(hits) <= n:
        return sorted(hits, key=lambda h: (h.thread_path, h.ordinal, h.sub))
    best = hits[0]
    chosen = [best]
    ordered = sorted(hits, key=lambda h: (h.thread_path != "", h.thread_path, h.ordinal, h.sub))
    for candidate in (ordered[0], ordered[-1]):
        if candidate.turn_pk not in {c.turn_pk for c in chosen} and len(chosen) < n:
            chosen.append(candidate)
    for h in hits:
        if len(chosen) >= n:
            break
        if h.turn_pk not in {c.turn_pk for c in chosen}:
            chosen.append(h)
    return sorted(chosen, key=lambda h: (h.thread_path, h.ordinal, h.sub))


def _count(index: Index, term: Term, speakers: tuple[str, ...]) -> int:
    total = 0
    for columns, speaker_filter in _column_groups(speakers):
        expr = f"{{{' '.join(columns)}}}: {term.fts()}" if columns else term.fts()
        sql = "SELECT COUNT(*) FROM turn_fts JOIN turn t ON t.id = turn_fts.rowid WHERE turn_fts MATCH ?"
        params: list = [expr]
        if speaker_filter:
            sql += " AND t.speaker IN (%s)" % ",".join("?" * len(speaker_filter))
            params.extend(speaker_filter)
        try:
            total += int(index.db.execute(sql, params).fetchone()[0])
        except Exception:
            continue
    return total


def _clean_snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
