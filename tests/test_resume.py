"""Unit tests for resume.plan_resume / finalize_resume.

The fake harness mimics the subset of BitgnHarness used by resume.py:
- get_run(run_id)   -> GetRunResponse-shaped object
- get_trial(tid)    -> GetTrialResponse-shaped object
- submit_run(run_id, *, force) -> state string
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest

from bitgn.harness_pb2 import (
    TRIAL_STATE_NEW,
    TRIAL_STATE_RUNNING,
    TRIAL_STATE_DONE,
    TRIAL_STATE_ERROR,
)

from bitgn_contest_agent.resume import (
    ResumePlan,
    ResumedTrial,
    plan_resume,
    finalize_resume,
)


@dataclass
class _FakeTrialHead:
    trial_id: str
    task_id: str
    state: int
    score: float = 0.0
    error: str = ""


@dataclass
class _FakeGetRunResponse:
    run_id: str
    benchmark_id: str
    trials: List[_FakeTrialHead] = field(default_factory=list)


@dataclass
class _FakeGetTrialResponse:
    trial_id: str
    task_id: str = ""
    instruction: str = ""


class _FakeHarness:
    def __init__(self, run_resp: _FakeGetRunResponse, trials: dict | None = None):
        self._run_resp = run_resp
        self._trials = trials or {}
        self.submit_calls: list[tuple[str, bool]] = []

    def get_run(self, run_id: str):
        assert run_id == self._run_resp.run_id
        return self._run_resp

    def get_trial(self, trial_id: str):
        return self._trials[trial_id]

    def submit_run(self, run_id: str, *, force: bool = False) -> str:
        self.submit_calls.append((run_id, force))
        return "RUN_STATE_EVALUATED"


def test_plan_resume_buckets_new_done_error():
    h = _FakeHarness(_FakeGetRunResponse(
        run_id="run-abc",
        benchmark_id="bitgn/pac1-prod",
        trials=[
            _FakeTrialHead("t-new-1",  "task-1", TRIAL_STATE_NEW),
            _FakeTrialHead("t-new-2",  "task-2", TRIAL_STATE_NEW),
            _FakeTrialHead("t-done-1", "task-3", TRIAL_STATE_DONE,  score=1.0),
            _FakeTrialHead("t-err-1",  "task-4", TRIAL_STATE_ERROR, error="boom"),
        ],
    ))

    plan = plan_resume(h, "run-abc")

    assert plan.run_id == "run-abc"
    assert plan.benchmark_id == "bitgn/pac1-prod"
    assert plan.done_count == 1
    assert plan.error_count == 1
    assert [t.trial_id for t in plan.pending] == ["t-new-1", "t-new-2"]
    assert [t.task_id for t in plan.pending] == ["task-1", "task-2"]
    assert all(t.instruction == "" for t in plan.pending)
    assert plan.stuck == []


def test_plan_resume_running_trial_fetches_instruction():
    run_resp = _FakeGetRunResponse(
        run_id="run-xyz",
        benchmark_id="bitgn/pac1-prod",
        trials=[
            _FakeTrialHead("t-run-1", "task-a", TRIAL_STATE_RUNNING),
            _FakeTrialHead("t-new-1", "task-b", TRIAL_STATE_NEW),
        ],
    )
    trials = {
        "t-run-1": _FakeGetTrialResponse(
            trial_id="t-run-1",
            task_id="task-a",
            instruction="Do the thing.",
        ),
    }
    h = _FakeHarness(run_resp, trials)

    plan = plan_resume(h, "run-xyz")

    assert [t.trial_id for t in plan.stuck] == ["t-run-1"]
    assert plan.stuck[0].instruction == "Do the thing."
    assert plan.stuck[0].task_id == "task-a"
    assert [t.trial_id for t in plan.pending] == ["t-new-1"]


def test_plan_resume_running_trial_uses_trialhead_task_id_when_gettrial_empty():
    """Guard against a server regression that returns empty task_id on GetTrial."""
    run_resp = _FakeGetRunResponse(
        run_id="run-xyz",
        benchmark_id="bitgn/pac1-prod",
        trials=[_FakeTrialHead("t-run-1", "task-a", TRIAL_STATE_RUNNING)],
    )
    trials = {
        "t-run-1": _FakeGetTrialResponse(trial_id="t-run-1", task_id="", instruction=""),
    }
    h = _FakeHarness(run_resp, trials)

    plan = plan_resume(h, "run-xyz")

    assert plan.stuck[0].task_id == "task-a"  # falls back to TrialHead.task_id
    assert plan.stuck[0].instruction == ""


def test_plan_resume_empty_run_all_done():
    run_resp = _FakeGetRunResponse(
        run_id="run-empty",
        benchmark_id="bitgn/pac1-dev",
        trials=[
            _FakeTrialHead("t-1", "task-1", TRIAL_STATE_DONE, score=1.0),
            _FakeTrialHead("t-2", "task-2", TRIAL_STATE_DONE, score=0.0),
        ],
    )
    h = _FakeHarness(run_resp)

    plan = plan_resume(h, "run-empty")

    assert plan.pending == []
    assert plan.stuck == []
    assert plan.done_count == 2
    assert plan.error_count == 0


def test_finalize_resume_defaults_to_force_true():
    h = _FakeHarness(_FakeGetRunResponse(run_id="r", benchmark_id="b"))

    state = finalize_resume(h, "r")

    assert state == "RUN_STATE_EVALUATED"
    assert h.submit_calls == [("r", True)]


def test_finalize_resume_respects_force_false():
    h = _FakeHarness(_FakeGetRunResponse(run_id="r", benchmark_id="b"))

    finalize_resume(h, "r", force=False)

    assert h.submit_calls == [("r", False)]


def test_bitgn_harness_exposes_get_run_get_trial_end_task_by_id():
    """The three RPCs resume.py needs must be reachable from BitgnHarness
    without touching its private connect client."""
    from bitgn_contest_agent.harness import BitgnHarness
    assert hasattr(BitgnHarness, "get_run")
    assert hasattr(BitgnHarness, "get_trial")
    assert hasattr(BitgnHarness, "end_task_by_id")
