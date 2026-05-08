"""Compute bench summary artifacts for a benchmark run directory.

Schema v1.2 (additive over v1.1): adds pcm_op aggregates — per-task
`pcm_ops`, `pcm_wall_ms`, `pcm_ops_by_op`, `pcm_ops_by_origin`, plus
overall `total_pcm_ops` and `total_pcm_wall_ms`. Surfaces the PCM
runtime pressure the dashboard counts as "steps" so "which tasks burn
op budget in prepass?" is answerable from the summary alone, without
parsing per-trace JSONL.

Schema v1.1 (additive over v1.0): extends overall and per-task records with
multi-run aggregates, token usage, harness_url, and divergence counts.

Older consumers tolerate unknown keys (Pydantic ConfigDict(extra="ignore"));
newer consumers reading older input fill missing fields via load_summary.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable

from bitgn_contest_agent.bench.aggregate import aggregate_runs
from bitgn_contest_agent.bench.divergence import is_divergent_step
from bitgn_contest_agent.adapter.pcm_tracing import origin_bucket
from bitgn_contest_agent.trace_schema import (
    TraceArch,
    TraceMeta,
    TraceOutcome,
    TracePcmOp,
    TraceStep,
    load_jsonl,
)


FROZEN_SCHEMA_KEYS = ("schema_version", "overall", "tasks")
BENCH_SUMMARY_SCHEMA_VERSION = "1.2.0"


def _iter_jsonl_files(logs_dir: Path) -> Iterable[Path]:
    return sorted(Path(logs_dir).rglob("*.jsonl"))


@dataclass(slots=True)
class _RunStats:
    """Per-run trace-extraction output.

    Holds everything `summarize` needs from a single JSONL file so that
    the aggregation loop reads like business logic rather than tuple
    index arithmetic. Grew from a 9-tuple once pcm_op stats pushed the
    field count past what positional returns could carry legibly.
    """
    task_id: str
    score: float
    steps: int
    meta: TraceMeta
    outcome: TraceOutcome
    divergence_steps: list[int]
    step_texts: list[str]
    step_wall_ms_sum: int
    arch_present: bool
    pcm_ops: int = 0
    pcm_wall_ms: int = 0
    pcm_ops_by_op: dict[str, int] = field(default_factory=dict)
    pcm_ops_by_origin: dict[str, int] = field(default_factory=dict)


def _extract_run(path: Path) -> _RunStats | None:
    meta: TraceMeta | None = None
    outcome: TraceOutcome | None = None
    divergence_steps: list[int] = []
    step_texts: list[str] = []
    step_wall_ms_sum: int = 0
    arch_present: bool = False
    pcm_ops_count: int = 0
    pcm_wall_ms_total: int = 0
    pcm_by_op: dict[str, int] = defaultdict(int)
    pcm_by_origin: dict[str, int] = defaultdict(int)
    try:
        for rec in load_jsonl(path):
            if isinstance(rec, TraceMeta):
                meta = rec
            elif isinstance(rec, TraceStep):
                ns = rec.next_step or {}
                current_state = ns.get("current_state", "") if isinstance(ns, dict) else ""
                plan_brief = ns.get("plan_remaining_steps_brief", []) if isinstance(ns, dict) else []
                text = current_state + " " + " ".join(str(p) for p in plan_brief)
                if is_divergent_step(text):
                    divergence_steps.append(rec.step)
                if current_state:
                    step_texts.append(current_state)
                step_wall_ms_sum += rec.wall_ms
            elif isinstance(rec, TraceArch):
                arch_present = True
            elif isinstance(rec, TracePcmOp):
                pcm_ops_count += 1
                pcm_wall_ms_total += rec.wall_ms
                pcm_by_op[rec.op] += 1
                pcm_by_origin[origin_bucket(rec.origin)] += 1
            elif isinstance(rec, TraceOutcome):
                outcome = rec
    except (ValueError, json.JSONDecodeError):
        return None
    if meta is None or outcome is None:
        return None
    score = float(outcome.score) if outcome.score is not None else (
        1.0 if (outcome.reported == "OUTCOME_OK" and outcome.terminated_by == "report_completion") else 0.0
    )
    return _RunStats(
        task_id=meta.task_id,
        score=score,
        steps=outcome.total_steps,
        meta=meta,
        outcome=outcome,
        divergence_steps=divergence_steps,
        step_texts=step_texts,
        step_wall_ms_sum=step_wall_ms_sum,
        arch_present=arch_present,
        pcm_ops=pcm_ops_count,
        pcm_wall_ms=pcm_wall_ms_total,
        pcm_ops_by_op=dict(pcm_by_op),
        pcm_ops_by_origin=dict(pcm_by_origin),
    )


def summarize(*, logs_dir: Path) -> Dict[str, Any]:
    by_task: dict[str, list[_RunStats]] = defaultdict(list)
    total_runs = 0
    total_passes = 0

    for path in _iter_jsonl_files(logs_dir):
        run = _extract_run(path)
        if run is None:
            continue
        by_task[run.task_id].append(run)
        total_runs += 1
        if run.score >= 1.0:
            total_passes += 1

    tasks_out: dict[str, dict[str, Any]] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    total_reasoning_tokens = 0
    total_pcm_ops = 0
    total_pcm_wall_ms = 0

    for task_id, entries in sorted(by_task.items()):
        runs = len(entries)
        passes = sum(1 for e in entries if e.score >= 1.0)
        med_steps = int(statistics.median(e.steps for e in entries)) if entries else 0
        passes_per_run = [1 if e.score >= 1.0 else 0 for e in entries]

        # Token sums from TraceOutcome
        task_input = sum(e.outcome.total_prompt_tokens for e in entries)
        task_output = sum(e.outcome.total_completion_tokens for e in entries)
        task_reasoning = sum(e.outcome.total_reasoning_tokens for e in entries)

        total_input_tokens += task_input
        total_output_tokens += task_output
        total_reasoning_tokens += task_reasoning

        harness_url = (entries[0].meta.harness_url or "") if entries else ""

        # Divergence: union of step indices across all runs, sorted and deduped
        divergence_all = sorted(set().union(*[set(e.divergence_steps) for e in entries]))

        # step_texts: union across runs (de-duped, order-preserving via dict)
        seen_texts: dict[str, None] = {}
        for e in entries:
            for txt in e.step_texts:
                if txt and txt not in seen_texts:
                    seen_texts[txt] = None
        step_texts_all = list(seen_texts.keys())

        # last_outcome: from the final entry's outcome.reported
        last_entry_outcome = entries[-1].outcome if entries else None
        last_outcome = (last_entry_outcome.reported if last_entry_outcome and last_entry_outcome.reported else "OUTCOME_OK")

        # last_latency_ms: wall_ms sum of the final entry
        last_latency_ms = entries[-1].step_wall_ms_sum if entries else 0

        # timed_out: True if ANY run was cancelled/timed out
        timed_out = any(
            (e.outcome.terminated_by == "cancel") or (e.outcome.error_kind == "CANCELLED")
            for e in entries
        )

        # category: no source yet — default to "other"
        category = "other"

        # pcm_op aggregates (v1.2): summed across runs so multi-run
        # tasks surface cumulative op pressure, matching the existing
        # token-sum convention.
        pcm_ops_total = sum(e.pcm_ops for e in entries)
        pcm_wall_ms_total = sum(e.pcm_wall_ms for e in entries)
        pcm_by_op: dict[str, int] = defaultdict(int)
        pcm_by_origin: dict[str, int] = defaultdict(int)
        for e in entries:
            for op, n in e.pcm_ops_by_op.items():
                pcm_by_op[op] += n
            for origin, n in e.pcm_ops_by_origin.items():
                pcm_by_origin[origin] += n

        total_pcm_ops += pcm_ops_total
        total_pcm_wall_ms += pcm_wall_ms_total

        tasks_out[task_id] = {
            "runs": runs,
            "passes": passes,
            "median_steps": med_steps,
            # v1.1 additive fields
            "passes_per_run": passes_per_run,
            "input_tokens": task_input,
            "output_tokens": task_output,
            "reasoning_tokens": task_reasoning,
            "harness_url": harness_url,
            "divergence_steps": divergence_all,
            # v1.1 additive (T1.10 evidence for triage)
            "step_texts": step_texts_all,
            "last_outcome": last_outcome,
            "last_latency_ms": last_latency_ms,
            "timed_out": timed_out,
            "category": category,
            # arch-logging additive field
            "arch_present": any(e.arch_present for e in entries),
            # v1.2 additive — pcm_op stats
            "pcm_ops": pcm_ops_total,
            "pcm_wall_ms": pcm_wall_ms_total,
            "pcm_ops_by_op": dict(pcm_by_op),
            "pcm_ops_by_origin": dict(pcm_by_origin),
        }

    # aggregate_runs expects a list of per-run summary dicts. In Phase 1 every
    # directory is single-run-per-task, so we synthesize one summary per task —
    # the bootstrap becomes "variance across tasks at this single run" and
    # degenerates to identity for a single-task directory. Phase 2's real
    # multi-run scoring will rework this call site with per-run summaries.
    task_summaries = []
    for task_id, entries in by_task.items():
        runs = len(entries)
        passes = sum(1 for e in entries if e.score >= 1.0)
        task_summaries.append({"overall": {"pass_rate": passes / runs if runs else 0.0}})

    agg = aggregate_runs(task_summaries, seed=12345)
    runs_per_task = max((len(e) for e in by_task.values()), default=0)

    overall_pass_rate = (total_passes / total_runs) if total_runs else 0.0

    return {
        "schema_version": BENCH_SUMMARY_SCHEMA_VERSION,
        "overall": {
            "total_runs": total_runs,
            "total_passes": total_passes,
            "pass_rate": overall_pass_rate,
            # v1.1 additive fields
            "runs_per_task": runs_per_task,
            "pass_rate_median": agg["pass_rate_median"],
            "pass_rate_min": agg["pass_rate_min"],
            "pass_rate_ci_lower": agg["pass_rate_ci_lower"],
            "pass_rate_ci_upper": agg["pass_rate_ci_upper"],
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_reasoning_tokens": total_reasoning_tokens,
            "trace_dir": str(Path(logs_dir).resolve()),
            "divergence_count": sum(len(t["divergence_steps"]) for t in tasks_out.values()),
            # v1.2 additive — pcm_op totals
            "total_pcm_ops": total_pcm_ops,
            "total_pcm_wall_ms": total_pcm_wall_ms,
        },
        "tasks": tasks_out,
    }


def load_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Upcast a v1.0, v1.1, or v1.2 bench_summary dict to the v1.2 shape.

    Older files are missing additive fields; this fills them with
    sensible defaults so callers can always assume the current (v1.2)
    structure. The returned `schema_version` preserves whatever the
    input carried — callers that branch on version should key off that,
    not assume the return is v1.2.
    """
    overall = dict(raw.get("overall", {}))
    pass_rate = overall.get("pass_rate", 0.0)

    # Fill v1.1 overall fields with defaults when absent
    overall.setdefault("runs_per_task", 0)
    overall.setdefault("pass_rate_median", pass_rate)
    overall.setdefault("pass_rate_min", pass_rate)
    overall.setdefault("pass_rate_ci_lower", pass_rate)
    overall.setdefault("pass_rate_ci_upper", pass_rate)
    overall.setdefault("total_input_tokens", 0)
    overall.setdefault("total_output_tokens", 0)
    overall.setdefault("total_reasoning_tokens", 0)
    overall.setdefault("trace_dir", "")
    overall.setdefault("divergence_count", 0)
    # v1.2 defaults
    overall.setdefault("total_pcm_ops", 0)
    overall.setdefault("total_pcm_wall_ms", 0)

    tasks = {}
    for task_id, task_data in raw.get("tasks", {}).items():
        t = dict(task_data)
        t.setdefault("passes_per_run", [])
        t.setdefault("input_tokens", 0)
        t.setdefault("output_tokens", 0)
        t.setdefault("reasoning_tokens", 0)
        t.setdefault("harness_url", "")
        t.setdefault("divergence_steps", [])
        t.setdefault("step_texts", [])
        t.setdefault("last_outcome", "OUTCOME_OK")
        t.setdefault("last_latency_ms", 0)
        t.setdefault("timed_out", False)
        t.setdefault("category", "other")
        # v1.2 defaults
        t.setdefault("pcm_ops", 0)
        t.setdefault("pcm_wall_ms", 0)
        t.setdefault("pcm_ops_by_op", {})
        t.setdefault("pcm_ops_by_origin", {})
        tasks[task_id] = t

    return {
        "schema_version": raw.get("schema_version", "1.0.0"),
        "overall": overall,
        "tasks": tasks,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate JSONL traces into a frozen bench_summary")
    parser.add_argument("logs_dir", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = summarize(logs_dir=args.logs_dir)
    out_text = json.dumps(summary, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out_text, encoding="utf-8")
    else:
        print(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
