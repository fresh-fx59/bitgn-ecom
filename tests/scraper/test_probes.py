# tests/scraper/test_probes.py
"""Probe runner tests with a fake harness/PCM."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from bitgn_scraper.db import init_schema
from bitgn_scraper.probes import probe_instantiation


@dataclass
class _Started:
    instruction: str
    trial_id: str
    harness_url: str


@dataclass
class _End:
    score: float
    score_detail: list[str] = field(default_factory=list)


class _ScriptedHarness:
    """Returns canned (instruction, score, score_detail) per StartPlayground call."""

    def __init__(self, script: list[tuple[str, float, list[str]]]) -> None:
        self._script = script
        self._idx = 0
        self.start_calls = 0
        self.end_calls = 0
        self._pending_end: list[_End] = []

    def start_playground(self, req):  # noqa: ARG002
        instr, score, detail = self._script[self._idx]
        self._idx += 1
        self.start_calls += 1
        self._pending_end.append(_End(score=score, score_detail=detail))
        return _Started(instruction=instr, trial_id=f"trial_{self.start_calls}",
                        harness_url=f"https://vm-{self.start_calls}.example")

    def end_trial(self, req):  # noqa: ARG002
        self.end_calls += 1
        return self._pending_end.pop(0)


class _NoopPcm:
    """PCM that accepts writes and answers without doing anything."""
    def write(self, req):  # noqa: ARG002
        return None

    def answer(self, req):  # noqa: ARG002
        return None


def _factory(url: str):  # noqa: ARG001
    return _NoopPcm()


def test_probe_stops_at_p1_when_score_one(tmp_path: Path) -> None:
    """P1 returns score=1.0 → no further probes for this instantiation."""
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    harness = _ScriptedHarness([
        ("instruction X", 1.0, []),  # P1 wins
    ])

    n_probes = probe_instantiation(
        harness_client=harness,
        pcm_factory=_factory,
        task_id="t001",
        benchmark_id="bitgn/pac1-prod",
        instruction_hash="abc",
        known_rules={},
        db_path=db_path,
        run_diagnostic_p2b=False,
        run_diagnostic_p6=False,
    )
    assert n_probes == 1
    assert harness.start_calls == 1
    with sqlite3.connect(db_path) as cx:
        rows = cx.execute("SELECT probe_kind, score FROM probe_log ORDER BY probe_id").fetchall()
    assert rows == [("P1_empty", 1.0)]


def test_probe_extracts_expected_answer_at_p1(tmp_path: Path) -> None:
    """P1 returns the canonical expected-answer detail; P2 then uses it and wins."""
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    harness = _ScriptedHarness([
        ("instruction Y", 0.0, ["answer is incorrect. Expected: '1989-02-16'"]),
        ("instruction Y", 1.0, []),
    ])

    n_probes = probe_instantiation(
        harness_client=harness,
        pcm_factory=_factory,
        task_id="t001",
        benchmark_id="bitgn/pac1-prod",
        instruction_hash="abc",
        known_rules={},
        db_path=db_path,
        run_diagnostic_p2b=False,
        run_diagnostic_p6=False,
    )
    assert n_probes == 2
    with sqlite3.connect(db_path) as cx:
        kinds = [r[0] for r in cx.execute("SELECT probe_kind FROM probe_log ORDER BY probe_id")]
        rules = cx.execute("SELECT rule_kind, rule_value FROM scoring_rules").fetchall()
    assert kinds == ["P1_empty", "P2_extracted"]
    assert ("expected_answer", "1989-02-16") in {(r[0], r[1]) for r in rules}
