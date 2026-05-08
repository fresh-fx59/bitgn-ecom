"""Run the router over ingested PROD/DEV bench JSONs — no BitGN calls.

Walks every task in the supplied bench JSONs, calls router.route() on
its `bitgn_instruction`, and diffs the result against
artifacts/routing/expected_routing_table.csv.

Usage:
    uv run python scripts/offline_replay.py \\
        artifacts/bench/*_prod_runs1.json artifacts/bench/*_dev_*.json

Exit status:
    0 — routing matches expected table
    1 — one or more diffs detected (details on stderr)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from bitgn_contest_agent.router import load_router

EXPECTED = Path("artifacts/routing/expected_routing_table.csv")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("bench_files", nargs="+", type=Path)
    p.add_argument("--update", action="store_true",
                   help="Rewrite the expected table with the current routing")
    args = p.parse_args()

    router = load_router(
        Path("src/bitgn_contest_agent/skills").resolve()
    )
    observed: list[dict] = []
    for bp in args.bench_files:
        if not bp.exists():
            print(f"missing: {bp}", file=sys.stderr)
            continue
        data = json.loads(bp.read_text())
        tasks = data.get("tasks", {})
        for task_id, task_entry in tasks.items():
            instr = task_entry.get("bitgn_instruction") or task_entry.get("task_text") or ""
            if not instr:
                continue
            decision = router.route(instr)
            observed.append({
                "task_id": task_id,
                "source_bench": bp.name,
                "expected_category": decision.category,
                "expected_source": decision.source,
            })

    if args.update:
        with EXPECTED.open("w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["task_id", "source_bench", "expected_category", "expected_source"],
            )
            w.writeheader()
            for row in observed:
                w.writerow(row)
        print(f"wrote {len(observed)} rows to {EXPECTED}", file=sys.stderr)
        return 0

    expected_rows: dict[tuple[str, str], tuple[str, str]] = {}
    if EXPECTED.exists():
        with EXPECTED.open() as f:
            for row in csv.DictReader(f):
                key = (row["task_id"], row["source_bench"])
                expected_rows[key] = (row["expected_category"], row["expected_source"])

    diffs: list[str] = []
    for row in observed:
        key = (row["task_id"], row["source_bench"])
        expected = expected_rows.get(key)
        if expected is None:
            diffs.append(
                f"NEW: {row['source_bench']} {row['task_id']} -> "
                f"{row['expected_category']}/{row['expected_source']}"
            )
            continue
        if expected != (row["expected_category"], row["expected_source"]):
            diffs.append(
                f"DIFF: {row['source_bench']} {row['task_id']} "
                f"expected={expected[0]}/{expected[1]} "
                f"observed={row['expected_category']}/{row['expected_source']}"
            )

    if diffs:
        print(f"{len(diffs)} routing diffs:", file=sys.stderr)
        for d in diffs:
            print(f"  {d}", file=sys.stderr)
        print(
            "Intentional? Re-run with --update to accept the new routing.",
            file=sys.stderr,
        )
        return 1
    print(f"{len(observed)} tasks routed, zero diffs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
