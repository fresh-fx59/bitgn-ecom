# Resume Run Design

**Status:** proposed
**Date:** 2026-04-15
**Driver:** long-running local-model benchmarks (PROD, ~104 trials × minutes-per-trial) crash or stall mid-run; restarting from zero is prohibitively expensive. User wants explicit `--resume <run_id>` support anchored to the same `run_id` shown in the BitGN dashboard, plus tolerance for minute-scale LM Studio / PAC1 transient outages without killing the running process.

## Goals

1. Let the user relaunch a crashed benchmark with `run-benchmark --resume <run_id>` where `<run_id>` matches the BitGN dashboard and pick up only the trials the server still considers unfinished.
2. Keep the resume logic in a self-contained module so it can be cherry-picked to another branch with minimal surface area.
3. Survive minute-scale transient outages (LM Studio model crash/reload, PAC1 brief 502, internet drift) without tearing down the process, via a longer configurable backoff tail.

## Non-goals

- No automatic state file / auto-resume. User supplies the `run_id` explicitly.
- No attempt to salvage trials already in state `RUNNING` beyond a single `end_trial` call — the server wall-clock has been burning since the original `start_trial`.
- No multi-iteration resume. `--resume` resumes exactly one run/iteration, then exits.
- No retry of `start_trial` on `RUNNING` trials (server would reject — wall-clock already started).
- No changes to agent.py's step loop, backend, or tool schemas other than the backoff schedule constant.

## Server capability verified

Inspected `bitgn.harness_pb2` and `HarnessServiceClientSync`:
- `GetRun(run_id) → GetRunResponse` returns `trials[] TrialHead` with per-trial `state ∈ {NEW, RUNNING, DONE, ERROR}` plus `task_id`, `score`, `error`. Sufficient to compute the pending work set on resume.
- `GetTrial(trial_id) → GetTrialResponse` returns full `{instruction, task_id, state, score, error, ...}` — recovers per-trial metadata without needing `start_trial` (which would restart the wall-clock).
- `SubmitRunRequest.force = true` lets us finalize with some trials stuck in ERROR/NEW. Aligns with user's acceptance that "task evolution might be failed".

## Architecture

### New module: `src/bitgn_contest_agent/resume.py`

Self-contained. Only imports: stdlib + `BitgnHarness` + the harness proto message types already used elsewhere. Pure functions over a dataclass surface — no I/O beyond harness RPCs, no global state, no CLI logic. Can be cherry-picked to another branch by copying this file plus the tiny CLI branch described below.

```python
# src/bitgn_contest_agent/resume.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from bitgn.harness_pb2 import (
    GetRunRequest,
    GetTrialRequest,
    SubmitRunRequest,
    TRIAL_STATE_NEW,
    TRIAL_STATE_RUNNING,
    TRIAL_STATE_DONE,
    TRIAL_STATE_ERROR,
)


@dataclass(frozen=True, slots=True)
class ResumedTrial:
    trial_id: str
    task_id: str
    instruction: str   # may be empty for NEW trials that never called start_trial


@dataclass(frozen=True, slots=True)
class ResumePlan:
    run_id: str
    benchmark_id: str
    pending: List[ResumedTrial]   # state == NEW; safe to call start_trial
    stuck:   List[ResumedTrial]   # state == RUNNING; try end_trial once, skip on error
    done_count: int
    error_count: int


def plan_resume(harness, run_id: str) -> ResumePlan:
    """Query PAC1 for the current run state and bucket trials by resumability.

    Does NOT call start_trial. Does NOT mutate server state. Safe to call
    repeatedly (e.g. for a dry-run probe before the real resume).
    """
    resp = harness._harness.get_run(GetRunRequest(run_id=run_id))
    pending: List[ResumedTrial] = []
    stuck: List[ResumedTrial] = []
    done = err = 0
    for t in resp.trials:
        if t.state == TRIAL_STATE_DONE:
            done += 1
        elif t.state == TRIAL_STATE_ERROR:
            err += 1
        elif t.state == TRIAL_STATE_NEW:
            pending.append(ResumedTrial(trial_id=t.trial_id, task_id=t.task_id, instruction=""))
        elif t.state == TRIAL_STATE_RUNNING:
            # GetTrial fills in instruction so we can salvage it without start_trial
            gt = harness._harness.get_trial(GetTrialRequest(trial_id=t.trial_id))
            stuck.append(ResumedTrial(
                trial_id=t.trial_id,
                task_id=gt.task_id or t.task_id,
                instruction=gt.instruction or "",
            ))
    return ResumePlan(
        run_id=resp.run_id,
        benchmark_id=resp.benchmark_id,
        pending=pending,
        stuck=stuck,
        done_count=done,
        error_count=err,
    )


def finalize_resume(harness, run_id: str, *, force: bool = True) -> str:
    """Finalize the run on the leaderboard. force=True by default so partial
    runs can still submit."""
    return harness.submit_run(run_id, force=force)
```

**Interface rationale**
- The module reaches into `harness._harness` for `get_run`/`get_trial` (not currently exposed on `BitgnHarness`). Acceptable because (a) both modules are co-owned and (b) we deliberately avoid enlarging `BitgnHarness`'s public surface just for resume. Alternative: add two thin pass-throughs on `BitgnHarness`. I prefer the former to keep resume.py's footprint small and portable.
- `ResumedTrial.instruction` is empty string for NEW trials — those have not had `start_trial` called yet, so their runtime_url/instruction don't exist until we call `start_trial`. That's fine: the normal worker path calls `start_trial` before running anyway.

### CLI wiring: minimal branch in `run-benchmark`

In `cli.py` `tasks_for_iteration(run_index)`:

```python
def tasks_for_iteration(run_index: int) -> List[TaskSpec]:
    if resume_run_id is not None:
        plan = resume.plan_resume(harness, resume_run_id)
        rid = plan.run_id
        leaderboard_run_ids[run_index] = rid
        # stuck trials: try end_trial inline, mark failed on error; don't queue
        for st in plan.stuck:
            try:
                harness.end_task_by_id(st.trial_id)
            except Exception:
                pass  # server keeps it in ERROR; force-submit handles it
        return [TaskSpec(task_id=t.task_id, trial_id=t.trial_id, ...) for t in plan.pending]
    # existing fresh-run path
    rid, trial_ids = harness.start_run(name=LEADERBOARD_RUN_NAME)
    ...
```

In `finalize_iteration`:
```python
state = harness.submit_run(rid, force=bool(resume_run_id))
```

`--resume <run_id>` is parsed in `cmd_run_benchmark`; if provided, iteration count is forced to 1. This is the only CLI change.

Small helper added to `BitgnHarness`: `end_task_by_id(trial_id)` — a thin wrapper over `end_trial` that takes a bare trial_id (existing `end_task` takes a `StartedTask`). Alternative: call `_harness.end_trial` directly from the CLI branch. I prefer the wrapper for symmetry.

### Backoff changes in `agent.py`

Extend `_DEFAULT_BACKOFF_MS` with a configurable long tail:

```python
_DEFAULT_BACKOFF_MS: tuple[int, ...] = (500, 1500, 4000, 10000)

def _build_backoff_schedule() -> tuple[int, ...]:
    extra = int(os.getenv("AGENT_MAX_BACKOFF_SEC", "300"))  # default 5 min tail
    if extra <= 0:
        return _DEFAULT_BACKOFF_MS
    # append one 30s step and one extra-second step so the total window is:
    # 0.5 + 1.5 + 4 + 10 + 30 + AGENT_MAX_BACKOFF_SEC seconds per call.
    return _DEFAULT_BACKOFF_MS + (30_000, extra * 1000)
```

Rationale:
- Original schedule (16s total) is too tight for a real minute-scale LM Studio crash or a PAC1 brief outage. Observed v16 killed 12 in-flight tasks after 16s.
- We cannot retry forever because `TASK_TIMEOUT_SEC` (1800s) upper-bounds the whole trial. A 5-minute tail leaves room for multiple retries within that budget.
- Configurable via env so it can be tuned or disabled (`AGENT_MAX_BACKOFF_SEC=0`).

## Failure modes & handling

| Situation | Behavior |
|-----------|----------|
| LM Studio crash/reload mid-trial | Transient — `_call_backend_with_retry` backs off up to `AGENT_MAX_BACKOFF_SEC`. Process stays alive. |
| PAC1 brief 502 | Transient — same retry path (unchanged classification). |
| PC shutdown / process kill | Run state persists server-side. User relaunches with `--resume <run_id>`. Any in-flight trial becomes ERROR; remaining NEW trials execute; `submit_run(force=True)` finalizes. |
| User passes wrong `run_id` | `get_run` returns a server error; surface it and exit non-zero. No destructive action. |
| User passes a `run_id` whose run is already DONE | `plan.pending` and `plan.stuck` are both empty; we go straight to `submit_run(force=True)` which is idempotent at the server. |
| All trials already DONE but `submit_run` never happened | Same as above — finalize with no new work. |

## Testing

- Unit tests for `resume.py` with a fake harness: NEW-only, mixed NEW+DONE+ERROR+RUNNING, RUNNING trial whose `GetTrial` errors, empty run.
- Integration smoke: `run-benchmark` with a finished dev run's `run_id` should invoke `submit_run(force=True)` and exit cleanly.
- Backoff: unit test that `AGENT_MAX_BACKOFF_SEC=0` returns the short schedule and a positive value appends the two long steps.

## Portability to another branch

Cherry-pick surface is intentionally tight:
1. `src/bitgn_contest_agent/resume.py` (new, ~80 lines).
2. `BitgnHarness.end_task_by_id` helper (~5 lines in `harness.py`).
3. `cli.py` `--resume` flag + branch in `tasks_for_iteration` and `finalize_iteration` (~30 lines).
4. Backoff schedule change in `agent.py` (~8 lines).

Tests live under `tests/test_resume.py` (new file) and a small addition to the existing `agent` test file for the backoff schedule helper.

## Out of scope

- Auto-persistence of `run_id` to a local file.
- Resume across multi-iteration benchmark loops.
- Replaying per-trial logs locally from `GetTrial.logs` (server already keeps them).
- Any change to the tool-calling backend or harmony stripper.
