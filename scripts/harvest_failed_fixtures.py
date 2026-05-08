#!/usr/bin/env -S python3 -u
"""Harvest test fixtures from failed PROD tasks.

Reads trace JSONL files from a benchmark run, extracts data for failed
tasks (score < 1.0), and combines with workspace snapshots (if available)
to create self-contained test fixtures.

Each fixture contains:
  - task_text (the instruction the agent received)
  - intent_head (first 240 chars, stable across reshuffles)
  - score + score_detail (grader feedback with expected values)
  - skill + category (how the router classified it)
  - step_count, token usage
  - workspace snapshot path (if dumped)

Usage:
    # From latest run logs
    python scripts/harvest_failed_fixtures.py --log-dir logs/20260417_092434

    # Custom output
    python scripts/harvest_failed_fixtures.py --log-dir logs/20260417_092434 \
        --output artifacts/fixtures/failed_tasks.json

    # Also dump workspaces for failed tasks (connects to PROD)
    python scripts/harvest_failed_fixtures.py --log-dir logs/20260417_092434 \
        --dump-workspaces
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bitgn_contest_agent.trace_schema import (
    TraceArch,
    TraceMeta,
    TraceOutcome,
    TraceTask,
    load_jsonl,
)


def extract_fixture(trace_path: Path) -> Optional[Dict[str, Any]]:
    """Extract fixture data from a single task trace file."""
    meta: Optional[TraceMeta] = None
    task: Optional[TraceTask] = None
    outcome: Optional[TraceOutcome] = None
    skill: Optional[str] = None
    router_source: Optional[str] = None
    router_confidence: Optional[float] = None

    for record in load_jsonl(trace_path):
        if isinstance(record, TraceMeta):
            meta = record
        elif isinstance(record, TraceTask):
            task = record
        elif isinstance(record, TraceOutcome):
            outcome = record
        elif isinstance(record, TraceArch):
            if record.skill and skill is None:
                skill = record.skill
                router_source = record.source
                router_confidence = record.confidence

    if not meta or not outcome:
        return None

    # Only include failed tasks
    if outcome.score is not None and outcome.score >= 1.0:
        return None

    fixture: Dict[str, Any] = {
        "task_id": meta.task_id,
        "task_text": task.task_text if task else "",
        "intent_head": meta.intent_head or (task.task_text[:240] if task else ""),
        "benchmark": meta.benchmark,
        "model": meta.model,
        "score": outcome.score,
        "score_detail": outcome.score_detail or [],
        "skill": skill,
        "router_source": router_source,
        "router_confidence": router_confidence,
        "total_steps": outcome.total_steps,
        "total_llm_calls": outcome.total_llm_calls,
        "total_prompt_tokens": outcome.total_prompt_tokens,
        "total_completion_tokens": outcome.total_completion_tokens,
        "terminated_by": outcome.terminated_by,
        "reported_answer": outcome.reported,
        "error_kind": outcome.error_kind,
        "error_msg": outcome.error_msg,
        "trace_file": str(trace_path),
        "agent_commit": meta.agent_commit,
    }

    # Parse grader patterns from score_detail
    fixture["grader_patterns"] = _classify_grader_feedback(outcome.score_detail or [])

    return fixture


def _classify_grader_feedback(details: List[str]) -> List[Dict[str, str]]:
    """Classify score_detail strings into known grader patterns."""
    patterns = []
    for detail in details:
        d = detail.lower()
        if "answer is incorrect" in d:
            patterns.append({"type": "wrong_answer", "detail": detail})
        elif "missing file write" in d:
            patterns.append({"type": "missing_file_write", "detail": detail})
        elif "expected outcome" in d:
            patterns.append({"type": "wrong_outcome", "detail": detail})
        elif "expected no changes" in d:
            patterns.append({"type": "unexpected_changes", "detail": detail})
        elif "frontmatter mismatch" in d:
            patterns.append({"type": "frontmatter_mismatch", "detail": detail})
        elif "missing required reference" in d:
            patterns.append({"type": "missing_reference", "detail": detail})
        else:
            patterns.append({"type": "unknown", "detail": detail})
    return patterns


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest fixtures from failed PROD tasks")
    parser.add_argument("--log-dir", required=True, help="Directory with trace JSONL files")
    parser.add_argument("--output", default=None, help="Output JSON path")
    parser.add_argument("--dump-workspaces", action="store_true",
                        help="Also dump workspaces for failed tasks via ws_dump.py")
    parser.add_argument("--snapshot-dir", default="artifacts/ws_snapshots")
    parser.add_argument("--env-file", default=".worktrees/plan-b/.env")
    parser.add_argument("--benchmark", default=None)
    parser.add_argument("--include-passing", action="store_true",
                        help="Include passing tasks too (for full fixture set)")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    if not log_dir.exists():
        print(f"ERROR: {log_dir} not found", file=sys.stderr)
        sys.exit(1)

    # Find all trace files
    trace_files = sorted(log_dir.glob("*.jsonl"))
    if not trace_files:
        print(f"ERROR: no .jsonl files in {log_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(trace_files)} traces from {log_dir}...")

    fixtures: List[Dict[str, Any]] = []
    all_tasks = 0
    for trace_path in trace_files:
        all_tasks += 1
        fixture = extract_fixture(trace_path)
        if fixture is None and not args.include_passing:
            continue
        if fixture is not None:
            fixtures.append(fixture)

    # Sort by skill then task_id
    fixtures.sort(key=lambda f: (f.get("skill") or "", f["task_id"]))

    print(f"\nFound {len(fixtures)} failed tasks out of {all_tasks} total")

    # Summary by skill
    skill_counts: Dict[str, int] = {}
    for f in fixtures:
        s = f.get("skill") or "unrouted"
        skill_counts[s] = skill_counts.get(s, 0) + 1
    print("\nFailures by skill:")
    for skill, count in sorted(skill_counts.items()):
        print(f"  {skill}: {count}")

    # Summary by grader pattern
    pattern_counts: Dict[str, int] = {}
    for f in fixtures:
        for p in f.get("grader_patterns", []):
            t = p["type"]
            pattern_counts[t] = pattern_counts.get(t, 0) + 1
    print("\nFailures by grader pattern:")
    for ptype, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        print(f"  {ptype}: {count}")

    # Print each failure
    print("\n" + "=" * 60)
    for f in fixtures:
        print(f"\n{f['task_id']} [{f.get('skill', '?')}] score={f['score']}")
        print(f"  intent: {f['intent_head'][:100]}...")
        for p in f.get("grader_patterns", []):
            print(f"  grader: [{p['type']}] {p['detail'][:120]}")
        if f.get("reported_answer"):
            print(f"  answer: {f['reported_answer'][:120]}")
        print(f"  steps={f['total_steps']} tokens={f['total_prompt_tokens']}+{f['total_completion_tokens']}")

    # Save fixtures
    output_path = Path(args.output) if args.output else (
        log_dir.parent / "fixtures" / f"{log_dir.name}_failed.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False))
    print(f"\nFixtures saved to {output_path}")

    # Optionally dump workspaces
    if args.dump_workspaces and fixtures:
        failed_ids = [f["task_id"] for f in fixtures]
        print(f"\nDumping workspaces for {len(failed_ids)} failed tasks...")
        dump_cmd = [
            sys.executable, "scripts/ws_dump.py",
            "--task-id", *failed_ids,
            "--output", args.snapshot_dir,
            "--env-file", args.env_file,
        ]
        if args.benchmark:
            dump_cmd.extend(["--benchmark", args.benchmark])
        subprocess.run(dump_cmd, cwd=Path(__file__).resolve().parent.parent)

        # Update fixtures with snapshot paths
        for f in fixtures:
            snap_dir = Path(args.snapshot_dir) / f["task_id"] / "run_0"
            if snap_dir.exists():
                f["workspace_snapshot"] = str(snap_dir)

        # Re-save with snapshot paths
        output_path.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False))
        print(f"Updated fixtures with workspace paths: {output_path}")


if __name__ == "__main__":
    main()
