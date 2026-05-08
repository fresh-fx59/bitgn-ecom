"""bench_summary frozen-schema aggregator test."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.bench_summary import FROZEN_SCHEMA_KEYS, summarize


def _write_trace(path: Path, *, task_id: str, outcome: str, score: float, steps: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "kind": "meta",
                "agent_version": "0.0.7",
                "agent_commit": "x",
                "model": "gpt-5.3-codex",
                "backend": "openai_compat",
                "reasoning_effort": "medium",
                "benchmark": "bitgn/pac1-dev",
                "task_id": task_id,
                "task_index": 0,
                "started_at": "2026-04-10T00:00:00Z",
                "trace_schema_version": "1.0.0",
            }
        ),
        json.dumps({"kind": "task", "task_id": task_id, "task_text": "x"}),
        json.dumps(
            {
                "kind": "outcome",
                "terminated_by": "report_completion",
                "reported": outcome,
                "enforcer_bypassed": False,
                "error_kind": None,
                "total_steps": steps,
                "total_llm_calls": steps,
                "total_prompt_tokens": 100,
                "total_completion_tokens": 10,
                "total_cached_tokens": 0,
                "score": score,
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_summarize_reports_pass_rate_and_frozen_keys(tmp_path: Path) -> None:
    _write_trace(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=5)
    _write_trace(tmp_path / "t1__run1.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=6)
    _write_trace(tmp_path / "t2__run0.jsonl", task_id="t2", outcome="OUTCOME_NONE_CLARIFICATION", score=0.0, steps=3)
    _write_trace(tmp_path / "t2__run1.jsonl", task_id="t2", outcome="OUTCOME_NONE_CLARIFICATION", score=0.0, steps=4)

    summary = summarize(logs_dir=tmp_path)
    assert set(summary.keys()) == set(FROZEN_SCHEMA_KEYS)
    assert summary["tasks"]["t1"]["runs"] == 2
    assert summary["tasks"]["t1"]["passes"] == 2
    assert summary["tasks"]["t1"]["median_steps"] in (5, 6)
    assert summary["tasks"]["t2"]["passes"] == 0
    assert summary["overall"]["pass_rate"] == pytest.approx(0.5)
    assert summary["overall"]["total_runs"] == 4


def test_summarize_is_stable_across_runs(tmp_path: Path) -> None:
    _write_trace(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=5)
    a = summarize(logs_dir=tmp_path)
    b = summarize(logs_dir=tmp_path)
    assert a == b


import pytest  # bottom import so tests above can use pytest.approx


def test_summarize_emits_v1_1_additive_fields(tmp_path: Path) -> None:
    """summarize() must emit all v1.1 additive fields in overall and per-task."""
    _write_trace(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK", score=1.0, steps=5)

    out = summarize(logs_dir=tmp_path)

    # schema version — v1.2 is additive over v1.1, so v1.1 fields
    # remain present.
    assert out["schema_version"] == "1.2.0"

    # overall v1.1 additive fields
    overall = out["overall"]
    for key in (
        "runs_per_task",
        "pass_rate_median",
        "pass_rate_min",
        "pass_rate_ci_lower",
        "pass_rate_ci_upper",
        "total_input_tokens",
        "total_output_tokens",
        "total_reasoning_tokens",
        "trace_dir",
        "divergence_count",
    ):
        assert key in overall, f"missing overall key: {key}"

    assert overall["total_input_tokens"] == 100
    assert overall["total_output_tokens"] == 10
    assert overall["total_reasoning_tokens"] == 0
    assert overall["divergence_count"] == 0
    assert overall["runs_per_task"] == 1

    # per-task v1.1 additive fields
    t1 = out["tasks"]["t1"]
    for key in ("passes_per_run", "input_tokens", "output_tokens", "reasoning_tokens", "harness_url", "divergence_steps"):
        assert key in t1, f"missing per-task key: {key}"

    assert t1["input_tokens"] == 100
    assert t1["output_tokens"] == 10
    assert t1["reasoning_tokens"] == 0
    assert t1["harness_url"] == ""
    assert t1["divergence_steps"] == []
    assert t1["passes_per_run"] == [1]


def test_summarize_populates_divergence_count_and_steps(tmp_path: Path) -> None:
    """bench_summary.summarize should populate overall.divergence_count and
    per-task divergence_steps from TraceStep.next_step.current_state."""
    path = tmp_path / "t1__run0.jsonl"
    lines = [
        json.dumps({
            "kind": "meta",
            "agent_version": "0.0.7",
            "agent_commit": "x",
            "model": "gpt-5.3-codex",
            "backend": "openai_compat",
            "reasoning_effort": "medium",
            "benchmark": "bitgn/pac1-dev",
            "task_id": "t1",
            "task_index": 0,
            "started_at": "2026-04-11T00:00:00Z",
            "trace_schema_version": "1.0.0",
        }),
        json.dumps({"kind": "task", "task_id": "t1", "task_text": "x"}),
        # Step 1 — contains a divergence keyword in current_state.
        json.dumps({
            "kind": "step",
            "step": 1,
            "wall_ms": 10,
            "llm": {"latency_ms": 10, "prompt_tokens": 1, "completion_tokens": 1, "cached_tokens": 0, "retry_count": 0},
            "next_step": {
                "current_state": "Reading AGENTS.md because user instruction contradicts it.",
                "plan_remaining_steps_brief": ["list", "report"],
                "identity_verified": True,
                "function": {"tool": "list", "name": "/"},
            },
            "tool_result": {"ok": True, "bytes": 0, "wall_ms": 0, "truncated": False, "original_bytes": 0, "error": None, "error_code": None},
            "session_after": {"seen_refs_count": 0, "identity_loaded": False, "rulebook_loaded": False},
        }),
        # Step 2 — benign, no keyword.
        json.dumps({
            "kind": "step",
            "step": 2,
            "wall_ms": 10,
            "llm": {"latency_ms": 10, "prompt_tokens": 1, "completion_tokens": 1, "cached_tokens": 0, "retry_count": 0},
            "next_step": {
                "current_state": "Listing the sandbox directory.",
                "plan_remaining_steps_brief": ["report"],
                "identity_verified": True,
                "function": {"tool": "list", "name": "/sandbox"},
            },
            "tool_result": {"ok": True, "bytes": 0, "wall_ms": 0, "truncated": False, "original_bytes": 0, "error": None, "error_code": None},
            "session_after": {"seen_refs_count": 0, "identity_loaded": False, "rulebook_loaded": False},
        }),
        json.dumps({
            "kind": "outcome",
            "terminated_by": "report_completion",
            "reported": "OUTCOME_OK",
            "enforcer_bypassed": False,
            "error_kind": None,
            "total_steps": 2,
            "total_llm_calls": 2,
            "total_prompt_tokens": 2,
            "total_completion_tokens": 2,
            "total_cached_tokens": 0,
            "total_reasoning_tokens": 0,
            "score": 1.0,
        }),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = summarize(logs_dir=tmp_path)
    assert summary["tasks"]["t1"]["divergence_steps"] == [1]
    assert summary["overall"]["divergence_count"] == 1


def test_schema_version_is_1_2_0():
    from scripts.bench_summary import BENCH_SUMMARY_SCHEMA_VERSION
    assert BENCH_SUMMARY_SCHEMA_VERSION == "1.2.0"


def _write_trace_with_pcm_ops(
    path: Path,
    *,
    task_id: str,
    pcm_ops: list[dict],
    score: float = 1.0,
) -> None:
    """Write a minimal trace that includes pcm_op records between meta and outcome."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "kind": "meta",
            "agent_version": "0.0.7",
            "agent_commit": "x",
            "model": "gpt-5.3-codex",
            "backend": "openai_compat",
            "reasoning_effort": "medium",
            "benchmark": "bitgn/pac1-dev",
            "task_id": task_id,
            "task_index": 0,
            "started_at": "2026-04-16T00:00:00Z",
            "trace_schema_version": "1.0.0",
        }),
        json.dumps({"kind": "task", "task_id": task_id, "task_text": "x"}),
    ]
    for op in pcm_ops:
        rec = {
            "kind": "pcm_op",
            "op": op["op"],
            "path": op.get("path"),
            "bytes": op.get("bytes", 0),
            "wall_ms": op.get("wall_ms", 0),
            "ok": op.get("ok", True),
            "error_code": op.get("error_code"),
            "origin": op.get("origin"),
        }
        lines.append(json.dumps(rec))
    lines.append(json.dumps({
        "kind": "outcome",
        "terminated_by": "report_completion",
        "reported": "OUTCOME_OK",
        "enforcer_bypassed": False,
        "error_kind": None,
        "total_steps": 1,
        "total_llm_calls": 1,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cached_tokens": 0,
        "total_reasoning_tokens": 0,
        "score": score,
    }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_summarize_aggregates_pcm_ops_per_task(tmp_path: Path) -> None:
    """Per-task pcm_op stats: total count, by-op, by-origin, wall_ms_total.

    Origin bucketing collapses step:1, step:2, ... into a single "step"
    bucket so summaries are comparable across tasks with different step
    counts. Prepass, routed_preflight, and None (unlabeled) each keep
    their own bucket.
    """
    ops = [
        # prepass: identity bootstrap
        {"op": "tree", "path": "/", "wall_ms": 200, "origin": "prepass"},
        {"op": "read", "path": "AGENTS.md", "wall_ms": 100, "origin": "prepass"},
        {"op": "read", "path": "ROLES.md", "wall_ms": 90, "origin": "prepass"},
        {"op": "list", "path": "/10_entities", "wall_ms": 50, "origin": "prepass"},
        # routed_preflight
        {"op": "read", "path": "10_entities/hanna.md", "wall_ms": 80,
         "origin": "routed_preflight"},
        # step:1 and step:2 (both collapse into "step")
        {"op": "read", "path": "hanna.md", "wall_ms": 70, "origin": "step:1"},
        {"op": "answer", "path": None, "wall_ms": 40, "origin": "step:2"},
    ]
    _write_trace_with_pcm_ops(tmp_path / "t1__run0.jsonl", task_id="t1", pcm_ops=ops)

    summary = summarize(logs_dir=tmp_path)
    t1 = summary["tasks"]["t1"]

    assert t1["pcm_ops"] == 7
    assert t1["pcm_wall_ms"] == 200 + 100 + 90 + 50 + 80 + 70 + 40
    assert t1["pcm_ops_by_op"] == {"tree": 1, "read": 4, "list": 1, "answer": 1}
    assert t1["pcm_ops_by_origin"] == {
        "prepass": 4,
        "routed_preflight": 1,
        "step": 2,
    }


def test_summarize_pcm_ops_buckets_unknown_origin_as_other(tmp_path: Path) -> None:
    """Ops with origin=None (e.g. traces from before attribution landed,
    or off-path runtime calls) go into an "other" bucket so legacy
    traces still report something useful rather than vanishing."""
    ops = [
        {"op": "read", "path": "a.md", "wall_ms": 10, "origin": None},
        {"op": "read", "path": "b.md", "wall_ms": 10, "origin": None},
    ]
    _write_trace_with_pcm_ops(tmp_path / "t1__run0.jsonl", task_id="t1", pcm_ops=ops)

    t1 = summarize(logs_dir=tmp_path)["tasks"]["t1"]
    assert t1["pcm_ops"] == 2
    assert t1["pcm_ops_by_origin"] == {"other": 2}


def test_summarize_sums_pcm_ops_across_multiple_runs(tmp_path: Path) -> None:
    """Like tokens, pcm_op aggregates are summed across runs of the same
    task — not averaged — so a 3-run task reports 3× the per-run ops."""
    ops = [
        {"op": "read", "path": "a.md", "wall_ms": 10, "origin": "prepass"},
        {"op": "list", "path": "/", "wall_ms": 5, "origin": "step:1"},
    ]
    _write_trace_with_pcm_ops(tmp_path / "t1__run0.jsonl", task_id="t1", pcm_ops=ops)
    _write_trace_with_pcm_ops(tmp_path / "t1__run1.jsonl", task_id="t1", pcm_ops=ops)

    t1 = summarize(logs_dir=tmp_path)["tasks"]["t1"]
    assert t1["pcm_ops"] == 4
    assert t1["pcm_wall_ms"] == 30
    assert t1["pcm_ops_by_op"] == {"read": 2, "list": 2}
    assert t1["pcm_ops_by_origin"] == {"prepass": 2, "step": 2}


def test_summarize_reports_overall_pcm_totals(tmp_path: Path) -> None:
    ops_t1 = [
        {"op": "read", "path": "a.md", "wall_ms": 100, "origin": "prepass"},
        {"op": "read", "path": "b.md", "wall_ms": 50, "origin": "step:1"},
    ]
    ops_t2 = [
        {"op": "tree", "path": "/", "wall_ms": 200, "origin": "prepass"},
    ]
    _write_trace_with_pcm_ops(tmp_path / "t1__run0.jsonl", task_id="t1", pcm_ops=ops_t1)
    _write_trace_with_pcm_ops(tmp_path / "t2__run0.jsonl", task_id="t2", pcm_ops=ops_t2)

    overall = summarize(logs_dir=tmp_path)["overall"]
    assert overall["total_pcm_ops"] == 3
    assert overall["total_pcm_wall_ms"] == 350


def test_summarize_omits_pcm_fields_when_no_pcm_ops(tmp_path: Path) -> None:
    """Traces without any pcm_op records (e.g. pre-tracing or crashed-
    early runs) still get populated pcm_* fields with zero counts so the
    schema shape is stable — consumers shouldn't have to defend against
    KeyError when reading a field that's merely empty."""
    _write_trace(tmp_path / "t1__run0.jsonl", task_id="t1",
                 outcome="OUTCOME_OK", score=1.0, steps=1)

    t1 = summarize(logs_dir=tmp_path)["tasks"]["t1"]
    assert t1["pcm_ops"] == 0
    assert t1["pcm_wall_ms"] == 0
    assert t1["pcm_ops_by_op"] == {}
    assert t1["pcm_ops_by_origin"] == {}


def test_load_summary_fills_pcm_defaults_for_v1_1_input() -> None:
    """v1.1 bench_summary JSON files lack pcm_* keys; load_summary must
    fill them so code that always reads v1.2 fields doesn't crash."""
    from scripts.bench_summary import load_summary

    v1_1 = {
        "schema_version": "1.1.0",
        "overall": {"total_runs": 1, "total_passes": 1, "pass_rate": 1.0},
        "tasks": {"t1": {"runs": 1, "passes": 1, "median_steps": 5}},
    }
    out = load_summary(v1_1)
    assert out["overall"]["total_pcm_ops"] == 0
    assert out["overall"]["total_pcm_wall_ms"] == 0
    t1 = out["tasks"]["t1"]
    assert t1["pcm_ops"] == 0
    assert t1["pcm_wall_ms"] == 0
    assert t1["pcm_ops_by_op"] == {}
    assert t1["pcm_ops_by_origin"] == {}


def test_bench_summary_reports_arch_present(tmp_path) -> None:
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import (
        TraceMeta, TraceOutcome, TRACE_SCHEMA_VERSION,
    )
    from bitgn_contest_agent.arch_log import emit_arch
    from bitgn_contest_agent.arch_constants import ArchCategory

    run_dir = tmp_path / "run1"
    run_dir.mkdir()

    # Task A: has arch records
    path_a = run_dir / "tA__run0.jsonl"
    w = TraceWriter(path=path_a)
    w.write_meta(TraceMeta(
        agent_version="x", agent_commit="y", model="m", backend="b",
        reasoning_effort="medium", benchmark="bench",
        task_id="tA", task_index=0,
        started_at="2026-04-14T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    ))
    emit_arch(w, category=ArchCategory.SKILL_ROUTER, skill="finance-lookup")
    w.append_outcome(TraceOutcome(
        terminated_by="report_completion", reported="OUTCOME_OK",
        enforcer_bypassed=False, total_steps=1, total_llm_calls=1,
        total_prompt_tokens=0, total_completion_tokens=0, score=1.0,
    ))
    w.close()

    # Task B: no arch records
    path_b = run_dir / "tB__run0.jsonl"
    w = TraceWriter(path=path_b)
    w.write_meta(TraceMeta(
        agent_version="x", agent_commit="y", model="m", backend="b",
        reasoning_effort="medium", benchmark="bench",
        task_id="tB", task_index=1,
        started_at="2026-04-14T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    ))
    w.append_outcome(TraceOutcome(
        terminated_by="report_completion", reported="OUTCOME_OK",
        enforcer_bypassed=False, total_steps=1, total_llm_calls=1,
        total_prompt_tokens=0, total_completion_tokens=0, score=1.0,
    ))
    w.close()

    summary = summarize(logs_dir=run_dir)
    assert summary["tasks"]["tA"]["arch_present"] is True
    assert summary["tasks"]["tB"]["arch_present"] is False
