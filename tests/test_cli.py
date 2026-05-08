"""CLI argument parsing — no live API calls."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.cli import build_parser


def test_parser_run_task_requires_task_id() -> None:
    parser = build_parser()
    ns = parser.parse_args(["run-task", "--task-id", "t14"])
    assert ns.command == "run-task"
    assert ns.task_id == "t14"


def test_parser_run_benchmark_defaults() -> None:
    parser = build_parser()
    ns = parser.parse_args(["run-benchmark"])
    assert ns.command == "run-benchmark"
    assert ns.runs == 1
    assert ns.max_parallel is None  # falls through to config default


def test_parser_run_benchmark_accepts_output_path() -> None:
    parser = build_parser()
    ns = parser.parse_args(
        ["run-benchmark", "--runs", "3", "--output", "artifacts/bench/out.json"]
    )
    assert ns.runs == 3
    assert ns.output == "artifacts/bench/out.json"


def test_parser_rejects_unknown_command() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run-quarantine"])


def _make_v1_1_summary_with_failures(failures: dict[str, str]) -> str:
    """Produce a minimal v1.1 bench_summary JSON string where each task_id
    in `failures` is crafted to classify_failure() into the named cluster.

    Clusters supported:
      - "inbox"        — step_texts includes "/inbox/"
      - "wrong_action" — OUTCOME_OK + step_texts mentions "instead of"
      - "false_refusal"— OUTCOME_DENIED_SECURITY, category="other"
      - "timeout"      — timed_out=True
      - "calendar"     — grader_failed + category="calendar"
      - "other"        — fallback, benign text
    """
    import json as _json

    cluster_to_task: dict[str, dict] = {
        "inbox": {
            "step_texts": ["forgot to check /inbox/identity.md"],
            "last_outcome": "OUTCOME_OK",
            "last_latency_ms": 2000,
            "timed_out": False,
            "category": "other",
        },
        "wrong_action": {
            "step_texts": ["writing email draft instead of the scheduler call"],
            "last_outcome": "OUTCOME_OK",
            "last_latency_ms": 2000,
            "timed_out": False,
            "category": "other",
        },
        "false_refusal": {
            "step_texts": ["refusing for safety"],
            "last_outcome": "OUTCOME_DENIED_SECURITY",
            "last_latency_ms": 500,
            "timed_out": False,
            "category": "other",
        },
        "timeout": {
            "step_texts": [],
            "last_outcome": "OUTCOME_ERR_INTERNAL",
            "last_latency_ms": 240_000,
            "timed_out": True,
            "category": "other",
        },
        "calendar": {
            "step_texts": ["scheduling the meeting"],
            "last_outcome": "OUTCOME_OK",
            "last_latency_ms": 2000,
            "timed_out": False,
            "category": "calendar",
        },
        "other": {
            "step_texts": ["random benign reasoning"],
            "last_outcome": "OUTCOME_OK",
            "last_latency_ms": 2000,
            "timed_out": False,
            "category": "other",
        },
    }

    tasks: dict[str, dict] = {}
    for tid, cluster in failures.items():
        t = {
            "runs": 1,
            "passes": 0,  # failure → passes < runs
            "median_steps": 1,
            "passes_per_run": [0],
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "harness_url": "",
            "divergence_steps": [],
        }
        t.update(cluster_to_task[cluster])
        tasks[tid] = t

    summary = {
        "schema_version": "1.1.0",
        "overall": {
            "total_runs": len(tasks),
            "total_passes": 0,
            "pass_rate": 0.0,
            "runs_per_task": 1,
            "pass_rate_median": 0.0,
            "pass_rate_min": 0.0,
            "pass_rate_ci_lower": 0.0,
            "pass_rate_ci_upper": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_reasoning_tokens": 0,
            "trace_dir": "",
            "divergence_count": 0,
        },
        "tasks": tasks,
    }
    return _json.dumps(summary)


def test_triage_cli_single_summary(tmp_path, capsys):
    """triage <summary.json> prints cluster -> list of task_ids."""
    summary = tmp_path / "s.json"
    summary.write_text(_make_v1_1_summary_with_failures({
        "t02": "inbox", "t08": "false_refusal", "t30": "timeout",
    }))
    from bitgn_contest_agent.cli import main
    rc = main(["triage", str(summary)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "inbox" in out and "t02" in out
    assert "timeout" in out and "t30" in out


def test_triage_cli_diff_mode(tmp_path, capsys):
    """triage --before A.json --after B.json prints +/- changes per cluster."""
    before = tmp_path / "a.json"
    before.write_text(_make_v1_1_summary_with_failures({"t02": "inbox", "t08": "false_refusal"}))
    after = tmp_path / "b.json"
    after.write_text(_make_v1_1_summary_with_failures({"t08": "false_refusal", "t30": "timeout"}))
    from bitgn_contest_agent.cli import main
    rc = main(["triage", "--before", str(before), "--after", str(after)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "-t02" in out  # inbox cleared
    assert "+t30" in out  # timeout added


def test_smoke_tasks_are_fixed():
    from bitgn_contest_agent.bench.smoke import (
        SMOKE_TASKS,
        SMOKE_CEILING_SEC,
        SMOKE_MAX_PARALLEL,
        SMOKE_MAX_INFLIGHT_LLM,
    )
    assert SMOKE_TASKS == ["t02", "t42", "t41", "t15", "t43"]
    assert SMOKE_CEILING_SEC == 180
    assert SMOKE_MAX_PARALLEL == 5
    assert SMOKE_MAX_INFLIGHT_LLM == 8


def test_smoke_flag_forces_parameters(monkeypatch, tmp_path):
    """--smoke must override --max-parallel and --max-inflight-llm and
    force the hardcoded SMOKE_TASKS list."""
    # load_from_env requires these three env vars
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://fake")
    monkeypatch.setenv("CLIPROXY_API_KEY", "fake")

    captured: dict = {}

    def fake_runner(cfg, **kw):
        captured["cfg"] = cfg
        captured["kw"] = kw
        # Invoke the provider once to capture the smoke TaskSpec list.
        captured["tasks"] = kw["tasks_for_iteration"](0)
        return []  # empty results; no summarize() path

    monkeypatch.setattr(
        "bitgn_contest_agent.cli._run_tasks_and_summarize", fake_runner
    )

    from bitgn_contest_agent.cli import main

    out_path = tmp_path / "x.json"
    rc = main([
        "run-benchmark",
        "--benchmark", "bitgn/pac1-dev",
        "--smoke",
        "--max-parallel", "99",
        "--max-inflight-llm", "99",
        "--output", str(out_path),
    ])
    # Fake returned empty → 0/0 pass rate → rc == 0
    assert rc == 0

    from bitgn_contest_agent.bench.smoke import (
        SMOKE_TASKS,
        SMOKE_MAX_PARALLEL,
        SMOKE_MAX_INFLIGHT_LLM,
    )
    # --smoke must override the CLI --max-parallel=99 / --max-inflight-llm=99
    assert captured["cfg"].max_parallel_tasks == SMOKE_MAX_PARALLEL
    assert captured["cfg"].max_inflight_llm == SMOKE_MAX_INFLIGHT_LLM
    # --smoke must force the task list to SMOKE_TASKS (TaskSpec objects ordered)
    assert [t.task_id for t in captured["tasks"]] == SMOKE_TASKS
    # Smoke stays on the playground flow — no trial_id set.
    assert all(t.trial_id is None for t in captured["tasks"])


def test_parser_parallel_iterations_tri_state() -> None:
    """--parallel-iterations / --no-parallel-iterations override, default None."""
    parser = build_parser()
    ns = parser.parse_args(["run-benchmark"])
    assert ns.parallel_iterations is None
    ns = parser.parse_args(["run-benchmark", "--parallel-iterations"])
    assert ns.parallel_iterations is True
    ns = parser.parse_args(["run-benchmark", "--no-parallel-iterations"])
    assert ns.parallel_iterations is False


def test_cmd_run_benchmark_enables_parallel_iterations_for_runs_gt_1(
    monkeypatch, tmp_path,
):
    """Auto-default: runs>1 non-smoke → parallel_iterations=True;
    runs=1 → False; smoke → False even with runs>1."""
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://fake")
    monkeypatch.setenv("CLIPROXY_API_KEY", "fake")

    captured_flags: list[bool] = []

    def fake_runner(cfg, **kw):
        captured_flags.append(kw["parallel_iterations"])
        return []

    monkeypatch.setattr(
        "bitgn_contest_agent.cli._run_tasks_and_summarize", fake_runner
    )
    # Avoid touching real BitGN endpoints during config wiring.
    monkeypatch.setattr(
        "bitgn_contest_agent.cli._make_harness", lambda cfg: object()
    )
    monkeypatch.setattr(
        "bitgn_contest_agent.cli._make_backend", lambda cfg: object()
    )

    from bitgn_contest_agent.cli import main

    main(["run-benchmark", "--benchmark", "bitgn/pac1-dev", "--runs", "3"])
    assert captured_flags[-1] is True

    main(["run-benchmark", "--benchmark", "bitgn/pac1-dev", "--runs", "1"])
    assert captured_flags[-1] is False

    main([
        "run-benchmark", "--benchmark", "bitgn/pac1-dev",
        "--runs", "3", "--smoke",
    ])
    assert captured_flags[-1] is False

    main([
        "run-benchmark", "--benchmark", "bitgn/pac1-dev",
        "--runs", "3", "--no-parallel-iterations",
    ])
    assert captured_flags[-1] is False


def test_run_tasks_and_summarize_parallel_iterations_executes_concurrently(
    tmp_path,
):
    """Parallel iterations: iterations overlap in time and result order
    matches submission order even when iteration 0 takes longer than 1."""
    import threading
    import time as _time

    from bitgn_contest_agent.cli import _run_tasks_and_summarize
    from bitgn_contest_agent.config import AgentConfig
    from bitgn_contest_agent.orchestrator import TaskExecutionResult, TaskSpec

    # Minimal config — task timeout generous, no parallelism inside
    # iteration (1 task per iteration).
    cfg = AgentConfig(
        bitgn_api_key="x",
        cliproxy_base_url="http://x",
        cliproxy_api_key="x",
        model="x",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        log_dir=str(tmp_path),
        max_steps=1,
        max_parallel_tasks=1,
        max_inflight_llm=2,
        llm_http_timeout_sec=60,
        rate_limit_backoff_ms=(100,),
        task_timeout_sec=30,
        task_timeout_grace_sec=5,
        max_tool_result_bytes=1024,
    )

    iteration_start_times: dict[int, float] = {}
    iteration_start_lock = threading.Lock()

    def tasks_for_iteration(i: int):
        with iteration_start_lock:
            iteration_start_times[i] = _time.monotonic()
        return [TaskSpec(task_id=f"t{i}", task_index=0, task_text="")]

    # Patch the _run_single_task the orchestrator runner will invoke.
    from unittest.mock import patch

    def fake_single(cfg, harness, backend, task, run_id, run_index,
                    cancel_event, inflight_semaphore=None, metrics=None):
        # iteration 0 sleeps longer than 1 so parallel execution is
        # detectable: serial would finish 1 before 0 completes only if
        # they're actually concurrent.
        _time.sleep(0.3 if run_index == 0 else 0.05)
        return TaskExecutionResult(
            task_id=task.task_id, score=1.0, terminated_by="report_completion",
            error_kind=None, error_msg=None,
        )

    with patch("bitgn_contest_agent.cli._run_single_task", side_effect=fake_single):
        t0 = _time.monotonic()
        results = _run_tasks_and_summarize(
            cfg,
            harness=object(),
            backend=object(),
            run_id="unit",
            runs=3,
            output=None,
            tasks_for_iteration=tasks_for_iteration,
            parallel_iterations=True,
        )
        elapsed = _time.monotonic() - t0

    # Serial would be ~0.3 + 0.05 + 0.05 = 0.4s minimum. Parallel caps
    # at max(0.3, 0.05, 0.05) = 0.3s; give generous headroom for CI jitter.
    assert elapsed < 0.38, f"expected parallel execution, took {elapsed:.3f}s"
    # Result order preserved (iter0 task, iter1 task, iter2 task)
    assert [r.task_id for r in results] == ["t0", "t1", "t2"]


# ---------------------------------------------------------------------------
# Adapter profile → classifier timeout wiring
# ---------------------------------------------------------------------------


def test_apply_adapter_profile_seeds_classifier_timeout_from_adapter(monkeypatch):
    """qwen3.6 remote adapter declares classifier_timeout_sec=65 to match
    the neuraldeep gateway's 60s server cap. classifier.py reads the value
    from BITGN_CLASSIFIER_TIMEOUT_SEC directly (not through cfg), so the
    adapter value is wired by exporting the env var at CLI startup.

    Regression: without this, local 20B adapter values (300s) or the
    hardcoded 10s classifier default would stomp the gateway-tuned 65s."""
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://fake")
    monkeypatch.setenv("CLIPROXY_API_KEY", "fake")
    monkeypatch.setenv("AGENT_TOOLCALLING", "1")
    monkeypatch.setenv("AGENT_MODEL", "qwen3.6-35b-a3b")
    monkeypatch.delenv("BITGN_CLASSIFIER_TIMEOUT_SEC", raising=False)

    from bitgn_contest_agent.cli import _apply_adapter_profile
    from bitgn_contest_agent.config import load_from_env

    _apply_adapter_profile(load_from_env())

    import os as _os
    assert _os.environ["BITGN_CLASSIFIER_TIMEOUT_SEC"] == "65"


def test_apply_adapter_profile_env_wins_over_adapter_for_classifier_timeout(monkeypatch):
    """Explicit env setting must beat the adapter's value — same precedence
    rule used for llm_http_timeout_sec / max_parallel. The escape hatch
    stays useful for ad-hoc tuning runs."""
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://fake")
    monkeypatch.setenv("CLIPROXY_API_KEY", "fake")
    monkeypatch.setenv("AGENT_TOOLCALLING", "1")
    monkeypatch.setenv("AGENT_MODEL", "qwen3.6-35b-a3b")
    monkeypatch.setenv("BITGN_CLASSIFIER_TIMEOUT_SEC", "999")

    from bitgn_contest_agent.cli import _apply_adapter_profile
    from bitgn_contest_agent.config import load_from_env

    _apply_adapter_profile(load_from_env())

    import os as _os
    assert _os.environ["BITGN_CLASSIFIER_TIMEOUT_SEC"] == "999"
