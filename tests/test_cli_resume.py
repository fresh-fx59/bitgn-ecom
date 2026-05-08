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
        # Mirror the real runner: call tasks_for_iteration to populate
        # leaderboard_run_ids, then finalize. Ignore the returned tasks —
        # the stubbed harness returns zero-length lists.
        kw["tasks_for_iteration"](0)
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
        # Mirror the real runner: call tasks_for_iteration to populate
        # leaderboard_run_ids, then finalize. Ignore the returned tasks —
        # the stubbed harness returns zero-length lists.
        kw["tasks_for_iteration"](0)
        kw["finalize_iteration"](0, [])
        return []
    monkeypatch.setattr(cli, "_run_tasks_and_summarize", _fake_runner)

    cli._cmd_run_benchmark(_make_args(resume=None, runs=1))

    assert stub.submit_calls == [("run-fresh", False)]
