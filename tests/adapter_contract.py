"""The behavior every adapter must show. Each adapter's test module calls ``check_store``.

What it defends: refs are stable (ordinals identical across full and incremental
reads), the tree is well formed (exactly one main thread, child kinds valid), and
speakers are normalized. It does not pin harness wording or file layouts.
"""

from __future__ import annotations

from dataclasses import dataclass

from scry.adapters.base import Adapter, Checkpoint, SessionSource, StoreSpec, ThreadRead
from scry.model import SPEAKERS, THREAD_KINDS


@dataclass
class StoreReport:
    sessions: dict[str, dict[tuple[str, ...], ThreadRead]]


def _assert_thread(read: ThreadRead, start_ordinal: int, start_sub: int = 0) -> None:
    seen_int = start_ordinal
    seen_sub = start_sub
    for turn in read.turns:
        if turn.sub == 0:
            assert turn.ordinal == seen_int + 1, f"ordinal gap: expected {seen_int + 1}, got {turn.ordinal} in {read.meta.path}"
            seen_int = turn.ordinal
            seen_sub = 0
        else:
            assert turn.ordinal == seen_int, f"sub-turn {turn.ordinal}.{turn.sub} follows integer turn {seen_int} in {read.meta.path}"
            assert turn.sub == seen_sub + 1, f"sub-turn gap: expected {seen_int}.{seen_sub + 1}, got {turn.ordinal}.{turn.sub} in {read.meta.path}"
            seen_sub = turn.sub
        assert turn.speaker in SPEAKERS
        if turn.speaker == "tool":
            assert turn.tool is not None or turn.text, "tool turns name their tool or carry output"
    assert read.meta.kind in THREAD_KINDS
    assert (read.checkpoint.ordinal, read.checkpoint.sub) == (seen_int, seen_sub)


def check_store(adapter: Adapter, store: StoreSpec) -> StoreReport:
    sessions = list(adapter.discover(store))
    assert sessions, f"no sessions discovered in {store.location}"
    report = StoreReport(sessions={})
    for source in sessions:
        assert isinstance(source, SessionSource)
        assert source.store == store.name
        mains = [t for t in source.threads if t.path == ()]
        assert len(mains) == 1, f"{source.session_id}: exactly one main thread, found {len(mains)}"
        assert mains[0].kind == "main"
        for child in source.threads:
            if child.path:
                assert child.kind != "main"
                assert child.location != mains[0].location
        meta = adapter.session_meta(source)
        assert meta.store == store.name and meta.session_id == source.session_id
        reads: dict[tuple[str, ...], ThreadRead] = {}
        for thread in source.threads:
            full = adapter.read_thread(source, thread, None)
            _assert_thread(full, 0)
            assert full.meta.path == thread.path and full.meta.kind == thread.kind
            # A second read from the returned checkpoint yields nothing new.
            again = adapter.read_thread(source, thread, full.checkpoint)
            assert not again.reset, "unchanged source must not reset"
            assert again.turns == [], f"checkpoint resume re-emitted {len(again.turns)} turns"
            # A read from a mid-way checkpoint continues with identical ordinals and turns.
            cut = _record_boundary(full)
            if cut is not None:
                mid = Checkpoint(
                    fingerprint=full.checkpoint.fingerprint,
                    offset=full.turns[cut].offset or 0,
                    ordinal=full.turns[cut - 1].ordinal,
                    native_id=full.turns[cut - 1].native_id,
                    sub=full.turns[cut - 1].sub,
                )
                rest = adapter.read_thread(source, thread, mid)
                assert not rest.reset
                _assert_thread(rest, full.turns[cut - 1].ordinal, full.turns[cut - 1].sub)
                assert [(t.ordinal, t.sub) for t in rest.turns] == [(t.ordinal, t.sub) for t in full.turns[cut:]]
                assert [t.text for t in rest.turns] == [t.text for t in full.turns[cut:]]
            reads[thread.path] = full
        report.sessions[source.session_id] = reads
    return report


def _record_boundary(read: ThreadRead) -> int | None:
    """A cut index near the middle where turn[cut] starts a new source record.

    One record may yield several turns (a Claude user message holding tool results and
    text); resuming inside such a record is not a valid checkpoint, so cuts land only
    where the offset changes. Adapters resuming by native id (databases) have no offsets
    and any cut is valid for them.
    """
    n = len(read.turns)
    if n < 2:
        return None
    if all(t.offset is None for t in read.turns):
        return n // 2
    order = list(range(n // 2, n)) + list(range(n // 2 - 1, 0, -1))
    for cut in order:
        if read.turns[cut].offset is not None and read.turns[cut].offset != read.turns[cut - 1].offset:
            return cut
    return None
