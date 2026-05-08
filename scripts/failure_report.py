#!/usr/bin/env python3
"""failure_report — per-task failure digest from bench JSON + trace dir.

For every task in a benchmark run that failed (passes == 0 OR bitgn_score == 0),
print a markdown block combining:

  - task text (from TraceTask record)
  - routing decision (skill, source, confidence) from SKILL_ROUTER arch
  - final report_completion message + outcome_justification from last step
  - server score_detail from outcome record
  - aggregated validator arch results (REJECT / MISMATCH / CORRECTED) with reasons
  - T2 trigger counts

Usage:
    failure_report.py artifacts/bench/<bench>.json
    failure_report.py artifacts/bench/<bench>.json --task t033
    failure_report.py artifacts/bench/<bench>.json --json
    failure_report.py <run-dir>                      # all failed traces in dir

Resolution of trace directory:
    1. --trace-dir (CLI) if provided
    2. bench['overall']['trace_dir'] if present
    3. <path> itself if it is a directory
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# repo root on path so this script runs from the checkout
_here = Path(__file__).resolve()
_repo_root = _here.parent.parent
if str(_repo_root / "src") not in sys.path:
    sys.path.insert(0, str(_repo_root / "src"))

import heapq  # noqa: E402 — for top-N expensive pcm_ops

from bitgn_contest_agent.adapter.pcm_tracing import origin_bucket  # noqa: E402
from bitgn_contest_agent.arch_constants import (  # noqa: E402
    ArchCategory,
    ArchResult,
)
from bitgn_contest_agent.trace_schema import (  # noqa: E402
    TraceArch,
    TraceMeta,
    TraceOutcome,
    TracePcmOp,
    TraceStep,
    TraceTask,
    load_jsonl,
)


# Number of slowest pcm_ops to surface per failing task. Five is enough
# to see both the prepass workspace-walk hotspots (typically 2-3 slow
# reads) and the step-phase hot loop (1-2 expensive reads or answer),
# without turning every digest into a log dump.
PCM_TOP_N = 5


@dataclass
class FailureDigest:
    task_id: str
    task_text: str = ""
    model: str = ""
    agent_commit: str = ""
    score: Optional[float] = None
    score_detail: list[str] = field(default_factory=list)
    terminated_by: str = ""
    reported: str = ""
    total_steps: int = 0
    routing_skill: Optional[str] = None
    routing_source: Optional[str] = None
    routing_confidence: Optional[float] = None
    routing_vars: Optional[str] = None
    final_tool: Optional[str] = None
    final_message: str = ""
    final_justification: str = ""
    # aggregates over arch records
    reject_reasons: list[str] = field(default_factory=list)
    mismatch_reasons: list[str] = field(default_factory=list)
    corrected_count: int = 0
    t2_triggers: Counter = field(default_factory=Counter)
    t1_rules_fired: Counter = field(default_factory=Counter)
    # where the final write lived (if any)
    final_mutation_count: int = 0
    # pcm_op aggregates — surfaces runtime-layer pressure that can
    # otherwise only be seen by running `jq .kind=="pcm_op"` against
    # the trace file. `pcm_top_ops` holds the PCM_TOP_N slowest ops
    # by wall_ms (TracePcmOp instances, descending).
    pcm_ops_total: int = 0
    pcm_ops_by_origin: Counter = field(default_factory=Counter)
    pcm_top_ops: list[TracePcmOp] = field(default_factory=list)


def _digest_trace(path: Path) -> Optional[FailureDigest]:
    meta: Optional[TraceMeta] = None
    task_rec: Optional[TraceTask] = None
    outcome: Optional[TraceOutcome] = None
    last_step: Optional[TraceStep] = None
    arch_records: list[TraceArch] = []
    pcm_ops_total: int = 0
    pcm_ops_by_origin: Counter = Counter()
    # Min-heap of (wall_ms, insertion_index, pcm_op). Insertion index
    # is a tiebreaker so heapq doesn't try to compare TracePcmOp
    # instances when two ops share a wall_ms value. Keeping the heap
    # size capped at PCM_TOP_N avoids retaining every record for large
    # traces (100+ ops common).
    pcm_top_heap: list[tuple[int, int, TracePcmOp]] = []
    pcm_seq: int = 0
    try:
        for rec in load_jsonl(path):
            if isinstance(rec, TraceMeta):
                meta = rec
            elif isinstance(rec, TraceTask):
                task_rec = rec
            elif isinstance(rec, TraceOutcome):
                outcome = rec
            elif isinstance(rec, TraceStep):
                last_step = rec
            elif isinstance(rec, TraceArch):
                arch_records.append(rec)
            elif isinstance(rec, TracePcmOp):
                pcm_ops_total += 1
                pcm_ops_by_origin[origin_bucket(rec.origin)] += 1
                entry = (rec.wall_ms, pcm_seq, rec)
                pcm_seq += 1
                if len(pcm_top_heap) < PCM_TOP_N:
                    heapq.heappush(pcm_top_heap, entry)
                else:
                    heapq.heappushpop(pcm_top_heap, entry)
    except Exception as exc:  # noqa: BLE001 — resilient to partial traces
        print(f"# skip {path.name}: {exc}", file=sys.stderr)
        return None
    if meta is None:
        return None

    d = FailureDigest(
        task_id=meta.task_id,
        task_text=(task_rec.task_text if task_rec else meta.intent_head or ""),
        model=meta.model,
        agent_commit=meta.agent_commit,
    )
    d.pcm_ops_total = pcm_ops_total
    d.pcm_ops_by_origin = pcm_ops_by_origin
    # Descending wall_ms so consumers see slowest first.
    d.pcm_top_ops = [op for (_ms, _seq, op) in sorted(pcm_top_heap, reverse=True)]
    if outcome is not None:
        d.score = outcome.score
        d.score_detail = list(outcome.score_detail or [])
        d.terminated_by = outcome.terminated_by
        d.reported = outcome.reported or ""
        d.total_steps = outcome.total_steps

    if last_step is not None:
        ns = last_step.next_step or {}
        fn = ns.get("function") or {}
        d.final_tool = fn.get("tool")
        d.final_message = fn.get("message") or ""
        d.final_justification = ns.get("outcome_justification") or ""
        d.final_mutation_count = last_step.session_after.mutation_count

    for rec in arch_records:
        # routing — first SKILL_ROUTER that has a skill wins (single router call/task)
        if rec.category == ArchCategory.SKILL_ROUTER and d.routing_skill is None and rec.skill:
            d.routing_skill = rec.skill
            d.routing_source = rec.source.value if hasattr(rec.source, "value") else rec.source
            d.routing_confidence = rec.confidence
            d.routing_vars = rec.details

        if rec.category == ArchCategory.VALIDATOR_T2 and rec.trigger is not None and rec.result is None:
            # "fired" marker: trigger present, no result yet
            trig = rec.trigger.value if hasattr(rec.trigger, "value") else str(rec.trigger)
            d.t2_triggers[trig] += 1

        if rec.category == ArchCategory.VALIDATOR_T1 and rec.rule is not None:
            rule = rec.rule.value if hasattr(rec.rule, "value") else str(rec.rule)
            d.t1_rules_fired[rule] += 1

        if rec.result == ArchResult.CORRECTED:
            d.corrected_count += 1
        if rec.result == ArchResult.REJECT:
            for r in rec.reasons or []:
                d.reject_reasons.append(r)
        if rec.result == ArchResult.MISMATCH:
            for r in rec.reasons or []:
                d.mismatch_reasons.append(r)

    return d


def _is_failure(task_dict: dict) -> bool:
    s = task_dict.get("bitgn_score")
    if s is not None:
        return float(s) < 0.999
    return task_dict.get("passes", 0) == 0


def _resolve_trace_dir(
    path: Path, bench: Optional[dict], override: Optional[Path]
) -> Optional[Path]:
    if override is not None:
        return override
    if bench is not None:
        td = bench.get("overall", {}).get("trace_dir")
        if td:
            return Path(td)
    if path.is_dir():
        return path
    return None


def _iter_failure_traces(
    trace_dir: Path, task_ids: Optional[set[str]]
) -> Iterable[Path]:
    # logs/<stamp>/t021__run0.jsonl pattern
    for jsonl in sorted(trace_dir.glob("t*.jsonl")):
        if task_ids is None:
            yield jsonl
            continue
        tid = jsonl.name.split("__", 1)[0]
        if tid in task_ids:
            yield jsonl


def _render_md(d: FailureDigest) -> str:
    lines: list[str] = []
    lines.append(f"## {d.task_id} — score={d.score}")
    lines.append("")
    lines.append(f"- **task**: {d.task_text}")
    lines.append(f"- **model**: {d.model}  **commit**: {d.agent_commit}")
    lines.append(
        f"- **routing**: skill=`{d.routing_skill}` source={d.routing_source} "
        f"conf={d.routing_confidence}"
    )
    if d.routing_vars:
        lines.append(f"  - extracted: `{d.routing_vars}`")
    lines.append(
        f"- **terminated_by**: {d.terminated_by}  **reported**: {d.reported}  "
        f"**steps**: {d.total_steps}  **mutations**: {d.final_mutation_count}"
    )
    if d.score_detail:
        lines.append("- **server score_detail**:")
        for sd in d.score_detail:
            lines.append(f"  - {sd}")
    if d.final_tool == "report_completion":
        if d.final_message:
            lines.append(f"- **final message**: {d.final_message}")
        if d.final_justification:
            lines.append(f"- **justification**: {d.final_justification}")
    elif d.final_tool:
        lines.append(f"- **final tool**: {d.final_tool}")

    if d.t2_triggers or d.t1_rules_fired or d.corrected_count or d.reject_reasons:
        lines.append("- **validator activity**:")
        if d.t2_triggers:
            trigs = ", ".join(f"{k}×{v}" for k, v in d.t2_triggers.most_common())
            lines.append(f"  - T2 triggers: {trigs}")
        if d.t1_rules_fired:
            rules = ", ".join(f"{k}×{v}" for k, v in d.t1_rules_fired.most_common())
            lines.append(f"  - T1 rules fired: {rules}")
        if d.corrected_count:
            lines.append(f"  - CORRECTED: {d.corrected_count}")
        if d.reject_reasons:
            lines.append("  - REJECT reasons:")
            for r in d.reject_reasons:
                lines.append(f"    - {r}")
        if d.mismatch_reasons:
            lines.append("  - MISMATCH reasons:")
            for r in d.mismatch_reasons:
                lines.append(f"    - {r}")
    else:
        lines.append("- **validator activity**: (none recorded)")

    if d.pcm_ops_total:
        origin_str = ", ".join(
            f"{bucket}={count}"
            for bucket, count in sorted(d.pcm_ops_by_origin.items())
        )
        lines.append(f"- **pcm_ops**: {d.pcm_ops_total} ({origin_str})")
        lines.append(f"- **top pcm_ops** (by wall_ms):")
        for op in d.pcm_top_ops:
            path_str = f" {op.path}" if op.path else ""
            origin_str = f" origin={op.origin}" if op.origin else ""
            err = f" error={op.error_code}" if not op.ok else ""
            lines.append(
                f"  - {op.op}{path_str} — {op.wall_ms}ms{origin_str}{err}"
            )
    lines.append("")
    return "\n".join(lines)


def _render_json(digests: list[FailureDigest]) -> str:
    out = []
    for d in digests:
        out.append(
            {
                "task_id": d.task_id,
                "task_text": d.task_text,
                "model": d.model,
                "agent_commit": d.agent_commit,
                "score": d.score,
                "score_detail": d.score_detail,
                "terminated_by": d.terminated_by,
                "reported": d.reported,
                "total_steps": d.total_steps,
                "mutations": d.final_mutation_count,
                "routing": {
                    "skill": d.routing_skill,
                    "source": d.routing_source,
                    "confidence": d.routing_confidence,
                    "vars": d.routing_vars,
                },
                "final": {
                    "tool": d.final_tool,
                    "message": d.final_message,
                    "justification": d.final_justification,
                },
                "validator": {
                    "t2_triggers": dict(d.t2_triggers),
                    "t1_rules_fired": dict(d.t1_rules_fired),
                    "corrected_count": d.corrected_count,
                    "reject_reasons": d.reject_reasons,
                    "mismatch_reasons": d.mismatch_reasons,
                },
                "pcm_ops": {
                    "total": d.pcm_ops_total,
                    "by_origin": dict(d.pcm_ops_by_origin),
                    "top": [
                        {
                            "op": op.op,
                            "path": op.path,
                            "wall_ms": op.wall_ms,
                            "origin": op.origin,
                            "ok": op.ok,
                            "error_code": op.error_code,
                        }
                        for op in d.pcm_top_ops
                    ],
                },
            }
        )
    return json.dumps(out, indent=2, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="failure_report",
        description="Per-task failure digest from bench JSON + trace dir.",
    )
    ap.add_argument("path", help="bench JSON file or trace directory")
    ap.add_argument("--trace-dir", type=Path, default=None,
                    help="override trace dir (default: bench.overall.trace_dir)")
    ap.add_argument("--task", action="append", default=None,
                    help="filter to one task id (repeatable)")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of markdown")
    ap.add_argument("--all", action="store_true",
                    help="include passing tasks too (default: failures only)")
    args = ap.parse_args(argv)

    path = Path(args.path)
    bench: Optional[dict] = None
    failing_ids: Optional[set[str]] = None

    if path.is_file() and path.suffix == ".json":
        bench = json.loads(path.read_text())
        tasks = bench.get("tasks", {})
        if args.all:
            failing_ids = set(tasks.keys())
        else:
            failing_ids = {tid for tid, td in tasks.items() if _is_failure(td)}

    if args.task:
        # CLI --task always wins; also forces trace scan for that id even if bench lacks it
        failing_ids = set(args.task)

    trace_dir = _resolve_trace_dir(path, bench, args.trace_dir)
    if trace_dir is None or not trace_dir.is_dir():
        print(
            f"error: could not resolve trace directory (got {trace_dir}). "
            "Pass --trace-dir.",
            file=sys.stderr,
        )
        return 2

    digests: list[FailureDigest] = []
    for jsonl in _iter_failure_traces(trace_dir, failing_ids):
        d = _digest_trace(jsonl)
        if d is None:
            continue
        digests.append(d)

    if args.json:
        print(_render_json(digests))
        return 0

    if not digests:
        print(f"# no failing tasks in {path}")
        return 0

    print(f"# Failure report: {path.name} (trace_dir={trace_dir.name})")
    print(f"# {len(digests)} failing task(s)")
    print()
    for d in digests:
        print(_render_md(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
