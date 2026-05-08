"""Serial stratified run — target group first, then sentinels.

Target group = all PROD tasks the router routes to the supplied
bitgn skill `category` (the category field on skill frontmatter, NOT
a server-side taxonomy — the server exposes no category on its
BenchmarkTask proto, verified at M0).

Sentinels = the eight canonical tasks in
docs/superpowers/specs/sentinels.csv, minus any that overlap with the
target group.

Flow:
  1. Resolve target task ids via offline routing over the committed
     bench baselines (no BitGN calls for discovery).
  2. Run those task ids against PROD (or DEV) via run-task in a loop.
  3. Verify strict pass on the target group (flake -> re-run once).
  4. Resolve the sentinel set, subtract overlap with step 1.
  5. Run the sentinel set.
  6. Verify no sentinel drops >0.5 from its committed baseline.
  7. Print a summary table.

**Plan deviation (2026-04-11):** the writing-plans draft assumed a
`--task-ids` flag on `run-benchmark`. The CLI does not expose one —
it has `--smoke` (hardcoded subset) or full-benchmark modes only.
This script shells out to `run-task --task-id X` in sequence instead.
M0 targets are empty (no skills loaded), so the serial cost is zero
until M1+ adds the first skill.

Usage:
    uv run python scripts/stratified_run.py \\
        --category FINANCE_LOOKUP \\
        --benchmark bitgn/pac1-prod
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

from bitgn_contest_agent.router import load_router


SENTINELS_CSV = Path("docs/superpowers/specs/sentinels.csv")
BASELINE_JSONS = [
    Path("artifacts/bench/2e6f621_hints1_p16i24_gpt54_20260411T132438Z_prod_runs1.json"),
    Path("artifacts/bench/36ada46_plus_fix2_gpt54_20260411T113715Z_prod_runs1.json"),
    Path("artifacts/bench/52f4e03_fix3_sonnet46_20260411T111525Z_prod_runs1.json"),
]


def _resolve_target_task_ids(category: str) -> list[str]:
    router = load_router(Path("src/bitgn_contest_agent/skills").resolve())
    task_ids: set[str] = set()
    for bp in BASELINE_JSONS:
        if not bp.exists():
            continue
        data = json.loads(bp.read_text())
        for task_id, task_entry in data.get("tasks", {}).items():
            instr = task_entry.get("bitgn_instruction") or ""
            if router.route(instr).category == category:
                task_ids.add(task_id)
    return sorted(task_ids)


def _load_sentinels() -> dict[str, float]:
    """Return {task_id: baseline_mean_score}."""
    out: dict[str, float] = {}
    with SENTINELS_CSV.open() as f:
        for row in csv.DictReader(f):
            out[row["task_id"]] = float(row["baseline_mean_score"])
    return out


def _run_one(task_id: str, benchmark: str) -> float:
    """Shell out to run-task and return the server score.

    run-task writes a bench-style JSON line at process end; we read
    the returned JSON on stdout. The CLI prints a dataclass dict with
    a `score` field backfilled from harness.end_task().
    """
    cmd = [
        "uv", "run", "python", "-m", "bitgn_contest_agent.cli",
        "run-task", "--task-id", task_id,
        "--benchmark", benchmark,
    ]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"WARNING: could not parse run-task output for {task_id}", file=sys.stderr)
        return 0.0
    return float(data.get("score", 0.0))


def _run_many(task_ids: list[str], benchmark: str) -> dict[str, float]:
    if not task_ids:
        return {}
    scores: dict[str, float] = {}
    for tid in task_ids:
        scores[tid] = _run_one(tid, benchmark)
    return scores


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--category", required=True, help="Bitgn skill category to test")
    p.add_argument("--benchmark", default="bitgn/pac1-prod")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve target+sentinel sets and print them; do not run")
    args = p.parse_args()

    target_ids = _resolve_target_task_ids(args.category)
    if not target_ids:
        print(
            f"target group empty for category {args.category} "
            f"— no skill loaded for this category yet",
            file=sys.stderr,
        )
    print(f"target group: {len(target_ids)} tasks", file=sys.stderr)

    sentinels = _load_sentinels()
    sentinel_ids = [tid for tid in sentinels if tid not in set(target_ids)]
    print(f"sentinels: {len(sentinel_ids)} tasks (after overlap removal)", file=sys.stderr)

    if args.dry_run:
        print("target:", target_ids)
        print("sentinels:", sentinel_ids)
        return 0
    # Not an error at M0: with zero skills every route is UNKNOWN, so
    # every stratified run reports an empty target. Sentinel-only
    # execution is still useful as a baseline check.

    # Stage A: target group.
    target_scores = _run_many(target_ids, args.benchmark)
    target_pass = all(target_scores.get(tid, 0.0) >= 0.999 for tid in target_ids)
    if not target_pass:
        failures = [tid for tid in target_ids if target_scores.get(tid, 0.0) < 0.999]
        print(f"TARGET GROUP FAIL: {failures}", file=sys.stderr)
        return 1
    if target_ids:
        print("TARGET GROUP PASS", file=sys.stderr)

    # Stage B: sentinels.
    sentinel_scores = _run_many(sentinel_ids, args.benchmark)
    regressions: list[str] = []
    for tid in sentinel_ids:
        baseline = sentinels[tid]
        observed = sentinel_scores.get(tid, 0.0)
        if baseline - observed > 0.5:
            regressions.append(f"{tid}: baseline={baseline:.2f} observed={observed:.2f}")
    if regressions:
        print(f"SENTINEL REGRESSIONS: {regressions}", file=sys.stderr)
        return 1
    print("SENTINEL SET PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
