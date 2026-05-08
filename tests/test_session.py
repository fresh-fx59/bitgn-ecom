"""Session state and loop detector."""
from __future__ import annotations

from bitgn_contest_agent.session import Session


def test_session_defaults_are_empty() -> None:
    s = Session()
    assert s.seen_refs == set()
    assert s.identity_loaded is False
    assert s.rulebook_loaded is False
    assert s.step == 0
    assert s.nudges_emitted == 0
    assert list(s.recent_calls) == []
    assert s.attempted_reads == set()
    assert s.verified_absent == set()
    assert s.loop_nudge_needed(("read", "AGENTS.md")) is False


def test_loop_detector_fires_when_same_tuple_seen_3_times_in_last_6() -> None:
    s = Session()
    tup = ("read", "AGENTS.md")
    other = ("list", "/")
    assert s.loop_nudge_needed(tup) is False  # 1 occurrence
    assert s.loop_nudge_needed(other) is False
    assert s.loop_nudge_needed(tup) is False  # 2 occurrences
    assert s.loop_nudge_needed(other) is False
    assert s.loop_nudge_needed(tup) is True   # 3 occurrences — nudge


def test_loop_detector_sliding_window_forgets_old_calls() -> None:
    s = Session()
    tup = ("search", "x")
    # Saturate the window with 6 distinct calls so the old tup is evicted.
    s.loop_nudge_needed(tup)
    for name in ["a", "b", "c", "d", "e", "f"]:
        s.loop_nudge_needed(("list", name))
    # tup has fallen out of the window; two more occurrences should not fire.
    assert s.loop_nudge_needed(tup) is False
    assert s.loop_nudge_needed(tup) is False


def test_nudge_budget_is_tracked_separately() -> None:
    s = Session()
    s.nudges_emitted = 2
    assert s.nudge_budget_remaining(max_nudges=2) == 0
    s.nudges_emitted = 0
    assert s.nudge_budget_remaining(max_nudges=2) == 2


def test_session_mutations_default_empty() -> None:
    s = Session()
    assert s.mutations == []


def test_session_record_mutation() -> None:
    s = Session()
    s.mutations.append(("write", "outbox/reply.md"))
    s.mutations.append(("delete", "50_finance/receipt_old.md"))
    assert len(s.mutations) == 2
    assert s.mutations[0] == ("write", "outbox/reply.md")
    assert s.mutations[1] == ("delete", "50_finance/receipt_old.md")


def test_session_tracks_attempted_reads() -> None:
    s = Session()
    assert s.attempted_reads == set()
    s.attempted_reads.add("AGENTS.md")
    s.attempted_reads.add("10_entities/cast/renate.md")
    assert "AGENTS.md" in s.attempted_reads
    assert len(s.attempted_reads) == 2


def test_session_tracks_verified_absent() -> None:
    s = Session()
    assert s.verified_absent == set()
    s.verified_absent.add("00_inbox/556_next-task.md")
    assert "00_inbox/556_next-task.md" in s.verified_absent


def test_session_new_fields_are_independent_of_seen_refs() -> None:
    s = Session()
    s.seen_refs.add("AGENTS.md")
    assert s.attempted_reads == set()
    assert s.verified_absent == set()
