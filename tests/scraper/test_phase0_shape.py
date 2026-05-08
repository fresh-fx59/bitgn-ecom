"""Phase 0 result-shape tests (no live API calls)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bitgn_scraper.phase0 import (
    LifecycleReport,
    RotationFinding,
    UrlLifetimeFinding,
    AnswerReplayFinding,
    RateLimitFinding,
    SizeSanityFinding,
    StateIsolationFinding,
    AutoTerminationFinding,
    serialize_report,
)


def test_serialize_report_round_trip() -> None:
    report = LifecycleReport(
        started_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        rotation=RotationFinding(
            task_id="t001",
            n_calls=20,
            distinct_instructions=5,
            sample_instructions=["a", "b", "c"],
        ),
        url_lifetime=UrlLifetimeFinding(
            trial_id="trial_x",
            harness_url="https://vm-x.eu.bitgn.com",
            probe_offsets_seconds=[0, 5, 30, 300, 1800],
            reachable_at_offset=[True, True, True, False, False],
        ),
        auto_termination=AutoTerminationFinding(
            trial_id="trial_y",
            probe_offsets_seconds=[600, 1800, 7200],
            reachable_at_offset=[True, False, False],
            inferred_max_lifetime_seconds=1800,
        ),
        state_isolation=StateIsolationFinding(
            wrote_path="/_probe.txt",
            second_trial_saw_write=False,
        ),
        answer_replay=AnswerReplayFinding(
            first_answer="alpha",
            second_answer="beta",
            graded_against="beta",
        ),
        rate_limit=RateLimitFinding(
            n_parallel_calls=20,
            n_throttled=0,
            throttle_status_codes=[],
        ),
        size_sanity=SizeSanityFinding(
            sampled_task_ids=["t001", "t005", "t010", "t020", "t050"],
            byte_totals=[1024, 4096, 8192, 16384, 32768],
            max_byte_total=32768,
        ),
    )
    blob = serialize_report(report)
    parsed = json.loads(blob)
    assert parsed["rotation"]["task_id"] == "t001"
    assert parsed["url_lifetime"]["reachable_at_offset"] == [True, True, True, False, False]
    assert parsed["state_isolation"]["second_trial_saw_write"] is False
    assert parsed["size_sanity"]["max_byte_total"] == 32768
    # ISO timestamp serialised as a string
    assert isinstance(parsed["started_at"], str)
    assert parsed["started_at"].startswith("2026-04-26T12:00:00")
