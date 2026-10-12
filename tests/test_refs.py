"""The ref grammar is the contract agents hold across sessions; these pin what it accepts and rejects."""

from __future__ import annotations

import pytest

from scry.model import Ref, RefError, TurnRange, looks_like_ref, parse_ref


@pytest.mark.parametrize(
    "text, store, session, agent, turns",
    [
        ("omp:01a07243", "omp", "01a07243", (), None),
        ("hermes.work:20260823_071850_daefe2", "hermes.work", "20260823_071850_daefe2", (), None),
        ("01a07243", None, "01a07243", (), None),
        ("omp:01a07243/Scout1", "omp", "01a07243", ("Scout1",), None),
        ("omp:01a07243/Scout1/Sub", "omp", "01a07243", ("Scout1", "Sub"), None),
        ("omp:01a07243/__advisor.learn@7", "omp", "01a07243", ("__advisor.learn",), (7, 7)),
        ("omp:01a07243@180-186", "omp", "01a07243", (), (180, 186)),
        ("omp:01a07243@17-", "omp", "01a07243", (), (17, 200)),
        ("omp:01a07243@-5", "omp", "01a07243", (), (196, 200)),
    ],
)
def test_parse_and_render_round_trip(text, store, session, agent, turns):
    ref = parse_ref(text)
    assert (ref.store, ref.session, ref.agent) == (store, session, agent)
    assert (ref.turns.resolve(200) if ref.turns else None) == turns
    assert str(ref) == text


@pytest.mark.parametrize("text", ["", "omp:", "omp:abc@0", "omp:abc@9-3", "omp:abc@x", "Omp!:abc", "omp:abc//x", "omp:a b"])
def test_rejects_malformed_refs_with_the_grammar(text):
    with pytest.raises(RefError) as err:
        parse_ref(text)
    assert "ref :=" in str(err.value) or "turn" in str(err.value)


def test_turn_range_clamps_to_the_thread():
    assert TurnRange(5, 50).resolve(10) == (5, 10)
    assert TurnRange(last=3).resolve(2) == (1, 2)
    assert TurnRange(11, None).resolve(10) == (11, 10)  # empty: first > last
    assert TurnRange(last=5).resolve(0) == (1, 0)


def test_navigation_up_the_address():
    ref = parse_ref("omp:01a07243/Scout1@12")
    assert str(ref.thread()) == "omp:01a07243/Scout1"
    assert str(ref.parent()) == "omp:01a07243/Scout1"
    assert str(ref.thread().parent()) == "omp:01a07243"
    assert ref.root().parent() is None
    assert str(ref.thread().at(3, 5)) == "omp:01a07243/Scout1@3-5"


@pytest.mark.parametrize(
    "text, expected",
    [("omp:01a07243", True), ("01a07243", True), ("fa3dadcd-a81d", True), ("alice", False), ("deploy", False), ("two words", False), ("omp:", False)],
)
def test_looks_like_ref_separates_ids_from_words(text, expected):
    assert looks_like_ref(text) is expected
