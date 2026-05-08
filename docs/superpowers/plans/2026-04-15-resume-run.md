# Resume Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `run-benchmark --resume <run_id>` so a crashed long-running benchmark can pick up where it left off on the same BitGN run, plus a configurable backoff tail so minute-scale LM Studio / PAC1 outages don't kill the process.

**Architecture:** A new self-contained module `src/bitgn_contest_agent/resume.py` wraps PAC1's `GetRun` / `GetTrial` / `SubmitRun(force=True)` to produce a `ResumePlan` the CLI feeds into the existing worker pool. A small helper on `BitgnHarness` exposes `end_trial` by bare `trial_id` for the RUNNING-trial salvage path. The backoff schedule in `agent.py` gets a configurable tail (`AGENT_MAX_BACKOFF_SEC`).

**Tech Stack:** Python 3.11+, pytest, protobuf (`bitgn.harness_pb2`), Connect-RPC (`HarnessServiceClientSync`), existing `BitgnHarness` wrapper.

---

## Source spec

`docs/superpowers/specs/2026-04-15-resume-run-design.md`

## File Structure

**New:**
- `src/bitgn_contest_agent/resume.py` — pure resume logic (dataclasses + two functions). ~90 lines. Depends only on `BitgnHarness` + harness proto. Portable.
- `tests/test_resume.py` — unit tests for `plan_resume`, `finalize_resume` with a fake harness.

**Modified:**
- `src/bitgn_contest_agent/harness.py` — add `BitgnHarness.end_task_by_id(trial_id)` + expose `get_run`/`get_trial` as thin pass-throughs so `resume.py` doesn't need to reach into `_harness`.
- `src/bitgn_contest_agent/config.py` — read `AGENT_MAX_BACKOFF_SEC` env and extend `rate_limit_backoff_ms`.
- `src/bitgn_contest_agent/cli.py` — add `--resume` flag, branch in `tasks_for_iteration`, pass `force=True` to `submit_run` when resuming, force `args.runs = 1`.
- `tests/test_config.py` (may not exist) or extend `tests/test_cli_run_benchmark.py` — test config-loader backoff tail.
- `tests/test_cli_run_benchmark.py` — test `--resume` path uses `plan_resume` and finalizes with `force=True`.

**Untouched:**
- `agent.py` — no change. Backoff schedule is already configurable via `AgentConfig.rate_limit_backoff_ms` which is already wired through `cli.py:275`. The change lives entirely in the config loader.
- Backend modules (`openai_toolcalling.py`, transient classification) — no change.

## Sanity baseline before starting

- [ ] **Step 0: Verify current tests pass**

Run: `.venv/bin/pytest -x -q tests/`
Expected: all green (ignore any PROD benchmark long-run tests if they exist).

- [ ] **Step 0b: Record current commit SHA**

Run: `git rev-parse --short HEAD`
Expected: `4189eb5` (the resume design commit) or a descendant.

---

## Task 1: `resume.py` module — ResumePlan dataclasses + `plan_resume` for terminal states (TDD)

Builds the skeleton of `resume.py` with states NEW, DONE, ERROR. RUNNING-trial handling comes in Task 2 (keeps the first test focused on the common path).

**Files:**
- Create: `src/bitgn_contest_agent/resume.py`
- Create: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test — NEW + DONE + ERROR bucketing**

Create `tests/test_resume.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_resume.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitgn_contest_agent.resume'`

- [ ] **Step 3: Create `src/bitgn_contest_agent/resume.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_resume.py -v`
Expected: PASS (1 test: `test_plan_resume_buckets_new_done_error`).

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/resume.py tests/test_resume.py
git commit -m "$(cat <<'EOF'
feat(resume): add resume module with plan_resume for NEW/DONE/ERROR

Self-contained module that buckets trials by server-reported state so a
crashed leaderboard run can pick up only what's still NEW. RUNNING-trial
salvage comes next.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `plan_resume` — RUNNING salvage via `get_trial`

**Files:**
- Modify: `tests/test_resume.py`
- (No implementation change — the Task 1 code already handles RUNNING; this task proves it with a test and adds the error-tolerance sub-case.)

- [ ] **Step 1: Write the failing test — RUNNING trial calls get_trial**

Append to `tests/test_resume.py`:

```python
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
```

- [ ] **Step 2: Run to verify both tests pass**

Run: `.venv/bin/pytest tests/test_resume.py -v`
Expected: all three new tests PASS plus the Task 1 test (4 total).

- [ ] **Step 3: Commit**

```bash
git add tests/test_resume.py
git commit -m "$(cat <<'EOF'
test(resume): cover RUNNING salvage + empty-run + task_id fallback

Locks in plan_resume's handling of RUNNING trials (instruction pulled via
GetTrial), its fallback to TrialHead.task_id when GetTrial returns empty,
and the all-DONE no-op case.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `finalize_resume` — force-submit wrapper (TDD)

**Files:**
- Modify: `tests/test_resume.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resume.py`:

```python
def test_finalize_resume_defaults_to_force_true():
    h = _FakeHarness(_FakeGetRunResponse(run_id="r", benchmark_id="b"))

    state = finalize_resume(h, "r")

    assert state == "RUN_STATE_EVALUATED"
    assert h.submit_calls == [("r", True)]


def test_finalize_resume_respects_force_false():
    h = _FakeHarness(_FakeGetRunResponse(run_id="r", benchmark_id="b"))

    finalize_resume(h, "r", force=False)

    assert h.submit_calls == [("r", False)]
```

- [ ] **Step 2: Run to verify both tests pass**

Run: `.venv/bin/pytest tests/test_resume.py -v`
Expected: 6 tests PASS. (The `finalize_resume` function was already written in Task 1.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_resume.py
git commit -m "$(cat <<'EOF'
test(resume): lock finalize_resume force=True default + override

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `BitgnHarness` pass-throughs — `get_run`, `get_trial`, `end_task_by_id`

Expose the three RPCs `resume.py` needs as thin wrappers on `BitgnHarness` so the module doesn't reach into `_harness`. Keeps the public surface small but honest.

**Files:**
- Modify: `src/bitgn_contest_agent/harness.py:205-220` (add three methods after `end_task`)
- Modify: `tests/test_resume.py` (swap fake for BitgnHarness-shaped assertion)

- [ ] **Step 1: Write the failing test — BitgnHarness exposes pass-throughs**

Append to `tests/test_resume.py`:

```python
def test_bitgn_harness_exposes_get_run_get_trial_end_task_by_id():
    """The three RPCs resume.py needs must be reachable from BitgnHarness
    without touching its private connect client."""
    from bitgn_contest_agent.harness import BitgnHarness
    assert hasattr(BitgnHarness, "get_run")
    assert hasattr(BitgnHarness, "get_trial")
    assert hasattr(BitgnHarness, "end_task_by_id")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_resume.py::test_bitgn_harness_exposes_get_run_get_trial_end_task_by_id -v`
Expected: FAIL — `AttributeError: type object 'BitgnHarness' has no attribute 'get_run'`

- [ ] **Step 3: Add three methods to `BitgnHarness`**

In `src/bitgn_contest_agent/harness.py`, first extend the import block (around line 31-38):

```python
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    GetRunRequest,
    GetTrialRequest,
    StartPlaygroundRequest,
    StartTrialRequest,
    SubmitRunRequest,
)
```

Then append the three methods just before `submit_run` (keep `submit_run` last since it's the terminal RPC):

```python
    def get_run(self, run_id: str):
        """Return the server's view of a run (trial states, scores, stats).

        Used by resume.py to compute which trials still need work after
        a process crash or relaunch. Does not mutate server state.
        """
        return self._harness.get_run(GetRunRequest(run_id=run_id))

    def get_trial(self, trial_id: str):
        """Return the server's view of a single trial (instruction, logs, state).

        Used by resume.py to recover the instruction text for RUNNING
        trials without calling start_trial (which would reset the
        server wall-clock).
        """
        return self._harness.get_trial(GetTrialRequest(trial_id=trial_id))

    def end_task_by_id(self, trial_id: str) -> Tuple[float, list[Any]]:
        """Grade a trial when we only have its id (no StartedTask).

        Symmetric with end_task() but takes a bare trial_id. Used by
        the resume path to attempt a best-effort grade of a RUNNING
        trial left over from a previous process.
        """
        resp = self._harness.end_trial(EndTrialRequest(trial_id=trial_id))
        return float(resp.score), list(resp.score_detail)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_resume.py -v`
Expected: 7 tests PASS.

- [ ] **Step 5: Also run the existing harness tests to ensure nothing broke**

Run: `.venv/bin/pytest tests/ -q -k "harness or resume"`
Expected: PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/harness.py tests/test_resume.py
git commit -m "$(cat <<'EOF'
feat(harness): expose get_run, get_trial, end_task_by_id for resume

Thin pass-throughs so resume.py can operate through the public
BitgnHarness surface instead of reaching into its private connect
client. No behavior change to existing callers.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Configurable backoff tail (`AGENT_MAX_BACKOFF_SEC`)

Extend the existing `AgentConfig.rate_limit_backoff_ms` via the config loader so a single env var lengthens the backoff schedule. No change to `agent.py` — it already consumes the tuple.

**Files:**
- Modify: `src/bitgn_contest_agent/config.py`
- Create: `tests/test_config_backoff.py`

- [ ] **Step 1: Write the failing test — default schedule unchanged when env unset**

Create `tests/test_config_backoff.py`:

```python
"""Tests for AGENT_MAX_BACKOFF_SEC extension of rate_limit_backoff_ms."""
from __future__ import annotations

import os

import pytest

from bitgn_contest_agent.config import _build_backoff_schedule


def test_build_backoff_schedule_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_BACKOFF_SEC", raising=False)
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000)


def test_build_backoff_schedule_disabled_by_zero(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "0")
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000)


def test_build_backoff_schedule_appends_tail_when_positive(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "300")
    # default head + 30s bridge + 300s tail
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000, 30_000, 300_000)


def test_build_backoff_schedule_ignores_negative(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "-5")
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000)


def test_build_backoff_schedule_rejects_nonnumeric(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "foo")
    with pytest.raises(ValueError):
        _build_backoff_schedule()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_config_backoff.py -v`
Expected: FAIL — `ImportError: cannot import name '_build_backoff_schedule'`

- [ ] **Step 3: Add `_build_backoff_schedule` to config.py and wire into the loader**

In `src/bitgn_contest_agent/config.py`, add above `load_from_env`:

```python
_DEFAULT_BACKOFF_MS: Tuple[int, ...] = (500, 1500, 4000, 10000)


def _build_backoff_schedule() -> Tuple[int, ...]:
    """Build the backend retry schedule, optionally with a long tail.

    The built-in schedule (16s total) is tuned for a healthy remote.
    Local inference (LM Studio on a single GPU) crashes or reloads for
    tens of seconds at a time, and PAC1 can return a brief 502 during
    a deploy — neither recovers inside 16s. Setting
    AGENT_MAX_BACKOFF_SEC=N appends a 30s bridge step and a final
    N-second step so a single backend call can survive an outage up
    to ~(16 + 30 + N) seconds before giving up. The trial-level
    TASK_TIMEOUT_SEC caps the overall trial budget, so there is no
    need to clamp AGENT_MAX_BACKOFF_SEC here.

    AGENT_MAX_BACKOFF_SEC=0 (or unset) keeps the short schedule —
    preserves the original behavior for frontier backends where a
    long tail would just waste time.
    """
    raw = os.environ.get("AGENT_MAX_BACKOFF_SEC")
    if raw is None:
        return _DEFAULT_BACKOFF_MS
    extra = int(raw)  # raises ValueError on non-numeric, which is desirable
    if extra <= 0:
        return _DEFAULT_BACKOFF_MS
    return _DEFAULT_BACKOFF_MS + (30_000, extra * 1000)
```

Also replace the hard-coded `rate_limit_backoff_ms` default in `load_from_env` so the env var actually takes effect. Change the call (near line 88):

```python
def load_from_env() -> AgentConfig:
    return AgentConfig(
        bitgn_api_key=_require("BITGN_API_KEY"),
        cliproxy_base_url=_require("CLIPROXY_BASE_URL"),
        cliproxy_api_key=_require("CLIPROXY_API_KEY"),
        benchmark=os.environ.get("BITGN_BENCHMARK", "bitgn/pac1-dev"),
        model=os.environ.get("AGENT_MODEL", "gpt-5.3-codex"),
        reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "medium"),
        max_steps=_int_env("MAX_STEPS", 40),
        task_timeout_sec=_int_env("TASK_TIMEOUT_SEC", 600),
        task_timeout_grace_sec=_int_env("TASK_TIMEOUT_GRACE_SEC", 20),
        llm_http_timeout_sec=_int_env("LLM_HTTP_TIMEOUT_SEC", 30),
        max_tool_result_bytes=_int_env("MAX_TOOL_RESULT_BYTES", 16384),
        max_parallel_tasks=_int_env("MAX_PARALLEL_TASKS", 4),
        max_inflight_llm=_int_env("MAX_INFLIGHT_LLM", 6),
        rate_limit_backoff_ms=_build_backoff_schedule(),
        log_dir=os.environ.get("LOG_DIR", "logs"),
    )
```

- [ ] **Step 4: Run backoff tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config_backoff.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Run the wider config/cli tests to ensure no regression**

Run: `.venv/bin/pytest tests/ -q -k "config or cli"`
Expected: PASS (the default case keeps behavior bit-identical when `AGENT_MAX_BACKOFF_SEC` is unset).

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/config.py tests/test_config_backoff.py
git commit -m "$(cat <<'EOF'
feat(config): AGENT_MAX_BACKOFF_SEC extends backoff schedule tail

Local LM Studio crashes and PAC1 brief 502s don't recover inside the
existing 16s window and were killing in-flight tasks. Opt-in long tail
lets the same process survive minute-scale outages without tearing
down. Default (env unset or 0) preserves frontier behavior.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: CLI `--resume <run_id>` flag

Wire `--resume` into the existing `run-benchmark` subcommand. When present: call `plan_resume`, feed `pending` into `tasks_for_iteration`, best-effort grade `stuck` trials, and finalize with `force=True`. Force `args.runs = 1`.

**Files:**
- Modify: `src/bitgn_contest_agent/cli.py` (argparse ~line 60, branch in `_cmd_run_benchmark` ~line 500-600)
- Create/extend: `tests/test_cli_resume.py`

- [ ] **Step 1: Write the failing test — argparse accepts `--resume`**

Create `tests/test_cli_resume.py`:

```python
"""End-to-end CLI tests for `run-benchmark --resume`.

The real BitGN server is not reachable in unit tests. We monkeypatch
`_make_harness` and the agent execution layer to feed canned responses
and assert the orchestrator did the right thing with them.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent.cli import build_parser


def test_run_benchmark_argparse_accepts_resume():
    parser = build_parser()
    args = parser.parse_args(["run-benchmark", "--resume", "run-abc123"])
    assert args.resume == "run-abc123"


def test_run_benchmark_resume_default_is_none():
    parser = build_parser()
    args = parser.parse_args(["run-benchmark"])
    assert args.resume is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_resume.py -v`
Expected: FAIL — either `AttributeError: 'Namespace' object has no attribute 'resume'` or `unrecognized arguments: --resume`.

- [ ] **Step 3: Add `--resume` to the argparse definition**

In `src/bitgn_contest_agent/cli.py`, in `build_parser` immediately after the existing `run_bench.add_argument(...)` block (around line 75, just before `tri = subs.add_parser(...)`):

```python
    run_bench.add_argument("--resume", default=None, metavar="RUN_ID",
                           help="resume a crashed leaderboard run by its BitGN run_id; "
                                "skips trials already DONE/ERROR and submits with force=True. "
                                "Implies --runs 1.")
```

- [ ] **Step 4: Run argparse test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli_resume.py -v`
Expected: 2 tests PASS.

- [ ] **Step 5: Commit the argparse slice**

```bash
git add src/bitgn_contest_agent/cli.py tests/test_cli_resume.py
git commit -m "$(cat <<'EOF'
feat(cli): --resume <run_id> argparse flag for run-benchmark

Accepts the flag; wiring it into execution comes next.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: CLI — wire `--resume` into `_cmd_run_benchmark`

**Files:**
- Modify: `src/bitgn_contest_agent/cli.py:500-600`
- Modify: `tests/test_cli_resume.py`

- [ ] **Step 1: Write the failing test — resume path calls plan_resume + force-submits**

Append to `tests/test_cli_resume.py`:

```python
from types import SimpleNamespace


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(
        command="run-benchmark",
        benchmark=None,
        runs=3,                # must be overridden to 1 when resume set
        max_parallel=None,
        output=None,
        log_dir=None,
        smoke=False,
        max_inflight_llm=None,
        parallel_iterations=None,
        resume="run-abc",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _StubHarness:
    """Captures submit_run calls; used to assert force=True on resume."""
    def __init__(self):
        self.submit_calls: list[tuple[str, bool]] = []
        self.end_calls: list[str] = []

    def submit_run(self, rid, *, force=False):
        self.submit_calls.append((rid, force))
        return "RUN_STATE_EVALUATED"

    def end_task_by_id(self, tid):
        self.end_calls.append(tid)
        return (0.0, [])

    # resume.plan_resume accesses these two:
    def get_run(self, run_id):
        return SimpleNamespace(
            run_id=run_id,
            benchmark_id="bitgn/pac1-prod",
            trials=[],  # all trials already DONE — nothing to do
        )

    def get_trial(self, tid):
        raise AssertionError("get_trial should not be called when no RUNNING trials")


def test_cmd_run_benchmark_resume_calls_plan_resume_and_force_submits(monkeypatch):
    """Smallest possible integration: resume an all-DONE run.

    Verifies the CLI (a) reuses the user-supplied run_id as the leaderboard
    id, (b) calls submit_run with force=True, (c) forces args.runs=1.
    """
    from bitgn_contest_agent import cli

    stub = _StubHarness()
    monkeypatch.setattr(cli, "_make_harness", lambda cfg: stub)
    monkeypatch.setattr(cli, "_make_backend", lambda cfg: MagicMock())
    monkeypatch.setattr(cli, "_resolve_config", lambda args: MagicMock(
        model="openai/gpt-oss-20b",
        log_dir="logs",
        benchmark="bitgn/pac1-prod",
        max_parallel_tasks=1,
        max_inflight_llm=1,
        max_steps=10,
        task_timeout_sec=60,
        task_timeout_grace_sec=5,
        llm_http_timeout_sec=30,
        max_tool_result_bytes=1024,
        rate_limit_backoff_ms=(0,),
        reasoning_effort="medium",
    ))

    # _run_tasks_and_summarize is the heavy path — stub it out entirely.
    # The resume path should still call finalize_iteration which is where
    # our force=True assertion lives.
    def _fake_runner(*a, **kw):
        # simulate the runner invoking finalize_iteration once with no results
        kw["finalize_iteration"](0, [])
        return []
    monkeypatch.setattr(cli, "_run_tasks_and_summarize", _fake_runner)

    rc = cli._cmd_run_benchmark(_make_args(resume="run-abc", runs=3))

    assert rc == 0  # 0/0 passed counts as pass for an empty run
    assert stub.submit_calls == [("run-abc", True)]


def test_cmd_run_benchmark_no_resume_uses_force_false(monkeypatch):
    """Regression guard: the non-resume path must NOT force-submit."""
    from bitgn_contest_agent import cli

    stub = _StubHarness()
    # For the non-resume path, the CLI calls start_run; fake it:
    stub.start_run = lambda name: ("run-fresh", [])  # zero trials — fastest path
    monkeypatch.setattr(cli, "_make_harness", lambda cfg: stub)
    monkeypatch.setattr(cli, "_make_backend", lambda cfg: MagicMock())
    monkeypatch.setattr(cli, "_resolve_config", lambda args: MagicMock(
        model="openai/gpt-oss-20b",
        log_dir="logs",
        benchmark="bitgn/pac1-prod",
        max_parallel_tasks=1,
        max_inflight_llm=1,
        max_steps=10,
        task_timeout_sec=60,
        task_timeout_grace_sec=5,
        llm_http_timeout_sec=30,
        max_tool_result_bytes=1024,
        rate_limit_backoff_ms=(0,),
        reasoning_effort="medium",
    ))

    def _fake_runner(*a, **kw):
        kw["finalize_iteration"](0, [])
        return []
    monkeypatch.setattr(cli, "_run_tasks_and_summarize", _fake_runner)

    cli._cmd_run_benchmark(_make_args(resume=None, runs=1))

    assert stub.submit_calls == [("run-fresh", False)]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli_resume.py -v`
Expected: FAIL — `AssertionError: [] == [('run-abc', True)]` (CLI currently ignores `--resume`).

- [ ] **Step 3: Modify `_cmd_run_benchmark` to branch on `args.resume`**

In `src/bitgn_contest_agent/cli.py`, add this import at the top of the file with the other `from bitgn_contest_agent.*` imports:

```python
from bitgn_contest_agent import resume as _resume_mod
```

Then in `_cmd_run_benchmark`, immediately after the existing `leaderboard_run_ids_lock = threading.Lock()` line (around line 541) and before the `if args.smoke:` block, insert:

```python
    # --resume pins a single iteration against an existing BitGN run_id.
    # Forces runs=1 because resume semantics only make sense for one run
    # (the run_id is already fixed server-side).
    resume_run_id: Optional[str] = getattr(args, "resume", None)
    if resume_run_id is not None:
        if args.smoke:
            print("--resume is incompatible with --smoke", file=sys.stderr)
            return 2
        if args.runs != 1:
            logging.getLogger(__name__).info(
                "--resume forces runs=1 (was %d)", args.runs,
            )
            args.runs = 1
```

Then replace the `else:` block's `tasks_for_iteration` + `finalize_iteration` (lines 558-600) with a resume-aware version:

```python
    else:
        _model_slug = cfg.model.rsplit("/", 1)[-1].replace(":", "-")
        LEADERBOARD_RUN_NAME = (
            f"aleksei_aksenov-ai_engineer_helper-bitgn-agent-{_model_slug}"
        )

        def tasks_for_iteration(run_index: int) -> List[TaskSpec]:
            if resume_run_id is not None:
                plan = _resume_mod.plan_resume(harness, resume_run_id)
                with leaderboard_run_ids_lock:
                    leaderboard_run_ids[run_index] = plan.run_id
                logging.getLogger(__name__).info(
                    "resume run_id=%s pending=%d stuck=%d done=%d error=%d",
                    plan.run_id, len(plan.pending), len(plan.stuck),
                    plan.done_count, plan.error_count,
                )
                # Best-effort grade stuck (RUNNING) trials inline. On error
                # the server keeps them in ERROR — force-submit handles it.
                for st in plan.stuck:
                    try:
                        harness.end_task_by_id(st.trial_id)
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "resume: end_task_by_id(%s) failed: %s",
                            st.trial_id, exc,
                        )
                return [
                    TaskSpec(
                        task_id=t.task_id,
                        task_index=i,
                        task_text="",
                        trial_id=t.trial_id,
                    )
                    for i, t in enumerate(plan.pending)
                ]

            rid, trial_ids = harness.start_run(name=LEADERBOARD_RUN_NAME)
            with leaderboard_run_ids_lock:
                leaderboard_run_ids[run_index] = rid
            return [
                TaskSpec(
                    task_id=tid,
                    task_index=i,
                    task_text="",
                    trial_id=tid,
                )
                for i, tid in enumerate(trial_ids)
            ]

        def finalize_iteration(run_index: int, _results: List[TaskExecutionResult]) -> None:
            with leaderboard_run_ids_lock:
                rid = leaderboard_run_ids.get(run_index)
            if rid is None:
                return
            try:
                state = harness.submit_run(rid, force=bool(resume_run_id))
                logging.getLogger(__name__).info(
                    "submitted run run_id=%s state=%s force=%s",
                    rid, state, bool(resume_run_id),
                )
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "submit_run failed for run_id=%s: %s", rid, exc,
                )
```

- [ ] **Step 4: Run resume tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_resume.py -v`
Expected: 4 tests PASS.

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/pytest -x -q tests/`
Expected: green. (A few slow integration tests may be skipped — that's fine.)

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/cli.py tests/test_cli_resume.py
git commit -m "$(cat <<'EOF'
feat(cli): run-benchmark --resume reuses BitGN run_id

Branches on args.resume: calls resume.plan_resume, queues only NEW
trials into the existing worker pool, best-effort grades RUNNING trials
inline, and finalizes with submit_run(force=True). Forces args.runs=1
because resume targets a single existing run_id.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Manual smoke against a real DEV run

Not a unit test — a 30-second operator verification that the wiring holds end-to-end.

- [ ] **Step 1: Kick off a fresh dev run and let it complete**

Run:
```bash
env BITGN_BENCHMARK=bitgn/pac1-dev .venv/bin/python -m bitgn_contest_agent run-benchmark --runs 1 --max-parallel 2 --output /tmp/resume-smoke-fresh.json
```
Expected: a `run_id` appears in the logs (search `submitted run run_id=`). Record it as `$RID`.

- [ ] **Step 2: Replay with `--resume $RID`**

Run:
```bash
env BITGN_BENCHMARK=bitgn/pac1-dev .venv/bin/python -m bitgn_contest_agent run-benchmark --resume "$RID" --max-parallel 2 --output /tmp/resume-smoke-replay.json
```
Expected: log shows `resume run_id=$RID pending=0 stuck=0 done=N error=M` and `submitted run run_id=$RID state=... force=True`. Exit code 0. `/tmp/resume-smoke-replay.json` exists with zero tasks attempted.

- [ ] **Step 3: If step 2 fails, stop and diagnose — do not commit workarounds**

Common failures to watch for:
- `BitGN GetRun HTTP 401`: harness auth interceptor isn't firing on `get_run`. Fix: verify `get_run` is routed through the same `HarnessServiceClientSync` instance as other RPCs.
- `BitGN SubmitRun HTTP 4xx "force required"`: the original run wasn't resumable from this state. Not a bug — an input problem.

- [ ] **Step 4: Commit the smoke evidence (optional)**

If helpful, paste the two log excerpts into a file like `docs/superpowers/notes/2026-04-15-resume-smoke.md` and commit. Skip if redundant.

---

## Task 9: Plan closeout

- [ ] **Step 1: Verify every spec section has a task**

Open `docs/superpowers/specs/2026-04-15-resume-run-design.md` and check:

| Spec section | Implemented in |
|---|---|
| `resume.py` module + dataclasses | Task 1, 2, 3 |
| `BitgnHarness.end_task_by_id` + `get_run`/`get_trial` exposure | Task 4 |
| `AGENT_MAX_BACKOFF_SEC` backoff tail | Task 5 |
| `--resume <run_id>` CLI flag | Task 6 |
| Resume branch in `tasks_for_iteration` | Task 7 |
| `force=True` submit in resume path | Task 7 |
| Failure modes table (wrong/already-DONE run_id) | covered by unit tests (empty run) + CLI smoke (Task 8) |
| Testing section | Tasks 1-7 |
| Portability surface | Intentional — structure of Tasks 1, 4, 5, 7 keeps resume.py + one helper + CLI branch cleanly separable |

- [ ] **Step 2: Full test run**

Run: `.venv/bin/pytest -q tests/`
Expected: all green.

- [ ] **Step 3: Push branch**

```bash
git push -u origin local-toolcalling-lfm2
```
(Do NOT merge to main — this branch carries experimental local-model changes.)

---

## Self-review notes

**Placeholder scan:** none found — every step that touches code shows the code.

**Type consistency:** `ResumedTrial` / `ResumePlan` shape is used identically across Tasks 1, 2, 3, 7. `harness.get_run`, `harness.get_trial`, `harness.end_task_by_id`, `harness.submit_run(rid, *, force)` signatures are consistent across Tasks 4 and 7. `_build_backoff_schedule` signature is stable between Task 5 definition and its single call site in `load_from_env`.

**Spec coverage:** table in Task 9 confirms every spec section lands in a task.

**Scope:** one spec, one implementation plan. No decomposition needed.
