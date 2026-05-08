#!/usr/bin/env -S .venv/bin/python3 -u
"""Harvest complete test cases from PROD benchmark traces + workspace snapshots.

Produces a self-contained fixture catalogue that pairs:
  - task instruction (from trace meta.intent_head)
  - workspace snapshot path (from artifacts/ws_snapshots/)
  - expected outcome + grader details (from trace outcome record)
  - routing/skill metadata (from trace arch records)
  - prepass chain (schema discovery, preflight results)

The catalogue enables:
  1. Offline replay: run agent against local workspace snapshot
  2. Variant generation: produce parameterized mutations of real tasks
  3. Regression testing: verify fixes without PROD access

Usage:
    # Harvest from the latest PROD run (eac8b36 baseline)
    python scripts/harvest_test_cases.py \
        --trace-dir logs/eac8b36_prod/20260419_075337 \
        --snapshot-dir artifacts/ws_snapshots \
        --output artifacts/test_cases/eac8b36_full.json

    # Harvest + dump any missing workspace snapshots
    python scripts/harvest_test_cases.py \
        --trace-dir logs/eac8b36_prod/20260419_075337 \
        --snapshot-dir artifacts/ws_snapshots \
        --dump-missing \
        --output artifacts/test_cases/eac8b36_full.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bitgn_contest_agent.trace_schema import (
    TraceArch,
    TraceMeta,
    TraceOutcome,
    TracePrepass,
    TraceStep,
    TraceTask,
    load_jsonl,
)


def _extract_test_case(trace_path: Path, snapshot_dir: Path) -> dict[str, Any] | None:
    """Extract a full test case from a single trace JSONL file."""
    meta: TraceMeta | None = None
    task: TraceTask | None = None
    outcome: TraceOutcome | None = None
    arch_records: list[dict[str, Any]] = []
    prepass_records: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []

    for record in load_jsonl(trace_path):
        if isinstance(record, TraceMeta):
            meta = record
        elif isinstance(record, TraceTask):
            task = record
        elif isinstance(record, TraceOutcome):
            outcome = record
        elif isinstance(record, TraceArch):
            arch_records.append({
                "skill": record.skill,
                "category": getattr(record, "category", None),
                "source": record.source,
                "confidence": record.confidence,
                "trigger": getattr(record, "trigger", None),
                "tag": getattr(record, "tag", None),
            })
        elif isinstance(record, TracePrepass):
            prepass_records.append({
                "cmd": record.cmd,
                "ok": record.ok,
                "bytes": record.bytes,
                "category": record.category,
                "query": record.query,
                "match_found": record.match_found,
                "match_file": record.match_file,
                "schema_roots": record.schema_roots,
                "skipped_reason": record.skipped_reason,
            })
        elif isinstance(record, TraceStep):
            ns = record.next_step
            steps.append({
                "step": record.step,
                "tool": ns.get("function", {}).get("tool") if isinstance(ns, dict) else None,
                "outcome_leaning": ns.get("outcome_leaning") if isinstance(ns, dict) else None,
                "current_state": (ns.get("current_state", "")[:200] if isinstance(ns, dict) else ""),
            })

    if not meta or not outcome:
        return None

    task_id = meta.task_id
    intent = meta.intent_head or (task.task_text[:240] if task else "")

    # Determine routing
    skill = None
    category = None
    router_source = None
    for a in arch_records:
        if a.get("skill") and skill is None:
            skill = a["skill"]
            category = a.get("category")
            router_source = a.get("source")

    # Check for workspace snapshot
    snap_path = snapshot_dir / task_id / "run_0" / "workspace"
    has_snapshot = snap_path.exists()

    # Extract expected grader data
    score = outcome.score
    score_detail = outcome.score_detail or []
    reported_outcome = outcome.reported

    # Classify the expected outcome type
    expected_outcome = _classify_expected(score, score_detail, reported_outcome)

    # Extract grounding refs from final step
    grounding_refs = []
    if steps:
        last = steps[-1]
        # Try to get refs from the report_completion call
        # They're in the trace outcome, not steps

    return {
        "task_id": task_id,
        "intent": intent,
        "category": category or "UNKNOWN",
        "skill": skill,
        "router_source": router_source,
        "score": score,
        "passed": score is not None and score >= 1.0,
        "score_detail": score_detail,
        "reported_outcome": reported_outcome,
        "expected": expected_outcome,
        "prepass": prepass_records,
        "step_count": len(steps),
        "steps_summary": steps[:3] + (steps[-2:] if len(steps) > 3 else []),
        "has_snapshot": has_snapshot,
        "snapshot_path": str(snap_path) if has_snapshot else None,
        "trace_file": str(trace_path),
        "agent_commit": meta.agent_commit,
        "benchmark": meta.benchmark,
    }


def _classify_expected(
    score: float | None,
    details: list[str],
    reported: str | None,
) -> dict[str, Any]:
    """Classify what the grader expected so we can verify locally."""
    result: dict[str, Any] = {"type": "unknown"}

    if score is not None and score >= 1.0:
        result["type"] = "pass"
        result["expected_outcome"] = reported
        return result

    # Parse grader feedback to understand expected behavior
    expected_writes: list[str] = []
    unexpected_writes: list[str] = []
    missing_refs: list[str] = []
    expected_answer: str | None = None
    expected_outcome_code: str | None = None

    for d in details:
        dl = d.lower()
        if "missing file write" in dl:
            # Extract path: "missing file write 'path/to/file.md'"
            path = _extract_quoted(d)
            if path:
                expected_writes.append(path)
            result["type"] = "missing_file_write"
        elif "unexpected file write" in dl:
            path = _extract_quoted(d)
            if path:
                unexpected_writes.append(path)
        elif "expected outcome" in dl:
            # "expected outcome OUTCOME_NONE_CLARIFICATION, got OUTCOME_OK"
            parts = d.split("expected outcome ")
            if len(parts) > 1:
                expected_outcome_code = parts[1].split(",")[0].strip()
            result["type"] = "wrong_outcome"
        elif "answer is incorrect" in dl or "answer missing" in dl:
            result["type"] = "wrong_answer"
        elif "missing required reference" in dl:
            path = _extract_quoted(d)
            if path:
                missing_refs.append(path)
            result["type"] = "missing_reference"
        elif "expected no changes" in dl:
            result["type"] = "unexpected_changes"

    if expected_writes:
        result["expected_writes"] = expected_writes
    if unexpected_writes:
        result["unexpected_writes"] = unexpected_writes
    if missing_refs:
        result["missing_refs"] = missing_refs
    if expected_outcome_code:
        result["expected_outcome"] = expected_outcome_code
    if expected_answer:
        result["expected_answer"] = expected_answer

    return result


def _extract_quoted(s: str) -> str | None:
    """Extract single-quoted value from a string."""
    start = s.find("'")
    if start < 0:
        return None
    end = s.find("'", start + 1)
    if end < 0:
        return None
    return s[start + 1:end]


def _build_category_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a summary grouped by category/skill."""
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        cat = c["category"]
        by_cat.setdefault(cat, []).append(c)

    summary = {}
    for cat, tasks in sorted(by_cat.items()):
        passed = sum(1 for t in tasks if t["passed"])
        failed = sum(1 for t in tasks if not t["passed"])
        # Collect unique intent patterns
        intents = [t["intent"][:80] for t in tasks]
        summary[cat] = {
            "total": len(tasks),
            "passed": passed,
            "failed": failed,
            "failure_rate": round(failed / len(tasks), 3) if tasks else 0,
            "sample_intents": intents[:5],
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest test cases from PROD traces")
    parser.add_argument("--trace-dir", required=True, help="Directory with .jsonl trace files")
    parser.add_argument("--snapshot-dir", default="artifacts/ws_snapshots",
                        help="Directory with workspace snapshots")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--dump-missing", action="store_true",
                        help="Dump missing workspace snapshots via ws_dump.py")
    args = parser.parse_args()

    trace_dir = Path(args.trace_dir)
    snapshot_dir = Path(args.snapshot_dir)

    if not trace_dir.exists():
        print(f"ERROR: {trace_dir} not found", file=sys.stderr)
        sys.exit(1)

    trace_files = sorted(trace_dir.glob("*.jsonl"))
    if not trace_files:
        print(f"ERROR: no .jsonl files in {trace_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(trace_files)} traces from {trace_dir}")

    cases: list[dict[str, Any]] = []
    for tf in trace_files:
        case = _extract_test_case(tf, snapshot_dir)
        if case:
            cases.append(case)

    cases.sort(key=lambda c: c["task_id"])

    # Stats
    passed = sum(1 for c in cases if c["passed"])
    failed = sum(1 for c in cases if not c["passed"])
    with_snapshot = sum(1 for c in cases if c["has_snapshot"])
    cats = Counter(c["category"] for c in cases)

    print(f"\n{'='*60}")
    print(f"Total: {len(cases)} tasks | Pass: {passed} | Fail: {failed}")
    print(f"With snapshot: {with_snapshot} | Missing: {len(cases) - with_snapshot}")
    print(f"\nBy category:")
    for cat, count in cats.most_common():
        cat_fail = sum(1 for c in cases if c["category"] == cat and not c["passed"])
        print(f"  {cat}: {count} ({cat_fail} failed)")

    # Build failure detail
    if failed:
        print(f"\nFailed tasks:")
        for c in cases:
            if not c["passed"]:
                print(f"  {c['task_id']} [{c['category']}] {c['expected']['type']}: {c['intent'][:80]}")

    # Category summary
    cat_summary = _build_category_summary(cases)

    # Output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    catalogue = {
        "source_trace_dir": str(trace_dir),
        "source_snapshot_dir": str(snapshot_dir),
        "total_tasks": len(cases),
        "passed": passed,
        "failed": failed,
        "category_summary": cat_summary,
        "test_cases": cases,
    }

    output_path.write_text(json.dumps(catalogue, indent=2, ensure_ascii=False))
    print(f"\nCatalogue saved to {output_path}")

    # List tasks missing snapshots (for optional dumping)
    missing = [c["task_id"] for c in cases if not c["has_snapshot"]]
    if missing:
        print(f"\nTasks missing workspace snapshots ({len(missing)}):")
        print(f"  {', '.join(missing[:20])}{'...' if len(missing) > 20 else ''}")
        if args.dump_missing:
            print(f"\nDumping {len(missing)} missing snapshots...")
            import subprocess
            for tid in missing:
                print(f"  Dumping {tid}...", end=" ", flush=True)
                result = subprocess.run(
                    [sys.executable, "scripts/ws_dump.py",
                     "--task-id", tid,
                     "--output", str(snapshot_dir)],
                    capture_output=True, text=True,
                    cwd=Path(__file__).resolve().parent.parent,
                )
                if result.returncode == 0:
                    print("OK")
                else:
                    print(f"FAIL: {result.stderr[:100]}")


if __name__ == "__main__":
    main()
