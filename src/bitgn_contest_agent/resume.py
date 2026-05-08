"""Resume a crashed PAC1 leaderboard run by trial_id.

Self-contained module. Depends only on stdlib + BitgnHarness + the
bitgn.harness_pb2 proto types already used elsewhere. Designed to be
cherry-pickable to another branch with minimal surface area.

No automatic persistence. The caller supplies `run_id` explicitly —
the same `run_id` visible in the BitGN dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from bitgn.harness_pb2 import (
    TRIAL_STATE_NEW,
    TRIAL_STATE_RUNNING,
    TRIAL_STATE_DONE,
    TRIAL_STATE_ERROR,
)


@dataclass(frozen=True, slots=True)
class ResumedTrial:
    """A trial recovered from the server that still needs work.

    `instruction` is empty for NEW trials (start_trial has not been
    called yet — the worker will call it the normal way). It is
    populated for RUNNING trials from GetTrial so the caller can
    salvage without re-starting the wall-clock.
    """
    trial_id: str
    task_id: str
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class ResumePlan:
    run_id: str
    benchmark_id: str
    pending: List[ResumedTrial]   # state == NEW; safe to call start_trial
    stuck:   List[ResumedTrial]   # state == RUNNING; try end_trial once
    done_count: int
    error_count: int


def plan_resume(harness, run_id: str) -> ResumePlan:
    """Query PAC1 for a run's current state and bucket trials by resumability.

    Does not mutate server state. Safe to call repeatedly.

    Args:
      harness: object exposing `get_run(run_id) -> GetRunResponse-like`
               and `get_trial(trial_id) -> GetTrialResponse-like`.
               `BitgnHarness` satisfies this.
      run_id:  the run_id visible in the BitGN dashboard.

    Returns:
      ResumePlan with pending (NEW) and stuck (RUNNING) trial lists.
    """
    resp = harness.get_run(run_id)
    pending: List[ResumedTrial] = []
    stuck: List[ResumedTrial] = []
    done = err = 0
    for t in resp.trials:
        if t.state == TRIAL_STATE_DONE:
            done += 1
        elif t.state == TRIAL_STATE_ERROR:
            err += 1
        elif t.state == TRIAL_STATE_NEW:
            pending.append(ResumedTrial(
                trial_id=t.trial_id,
                task_id=t.task_id,
                instruction="",
            ))
        elif t.state == TRIAL_STATE_RUNNING:
            gt = harness.get_trial(t.trial_id)
            stuck.append(ResumedTrial(
                trial_id=t.trial_id,
                task_id=gt.task_id or t.task_id,
                instruction=gt.instruction or "",
            ))
        # TRIAL_STATE_UNSPECIFIED: ignore (server shouldn't emit it)
    return ResumePlan(
        run_id=resp.run_id,
        benchmark_id=resp.benchmark_id,
        pending=pending,
        stuck=stuck,
        done_count=done,
        error_count=err,
    )


def finalize_resume(harness, run_id: str, *, force: bool = True) -> str:
    """Finalize the run on the leaderboard.

    force=True by default so partial runs (some trials still ERROR or NEW
    after the resume pass) can still submit. The PAC1 server's SubmitRun
    with force=True tolerates non-terminal trials.
    """
    return harness.submit_run(run_id, force=force)
