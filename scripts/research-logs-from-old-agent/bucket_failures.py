"""Group paired runs by a chosen dimension and show pass/fail counts.

Usage:
    python3 scripts/research-logs-from-old-agent/bucket_failures.py --by outcome
    python3 scripts/research-logs-from-old-agent/bucket_failures.py --by terminal_mode
    python3 scripts/research-logs-from-old-agent/bucket_failures.py --by task_id
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from legacy_loader import Run, load_runs


BUCKETS = {
    "outcome": lambda r: r.outcome or "<none>",
    "terminal_mode": lambda r: r.terminal_mode or "<none>",
    "task_id": lambda r: r.task_id,
    "task_family": lambda r: r.task_family or "<none>",
    "current_phase": lambda r: r.current_phase or "<none>",
    "finalization_ready": lambda r: str(r.finalization_ready),
    "respond_instructions_loaded": lambda r: str(r.respond_instructions_loaded),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--by",
        choices=list(BUCKETS),
        default="outcome",
        help="which field to group by",
    )
    args = ap.parse_args()

    runs = load_runs()
    total = len(runs)
    passed_total = sum(1 for r in runs if r.score >= 1.0)
    failed_total = total - passed_total

    print(f"loaded {total} paired runs (pass={passed_total}, fail={failed_total})")
    print(f"grouping by: {args.by}")
    print()

    key = BUCKETS[args.by]
    groups: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        groups[key(r)].append(r)

    print(f"{'bucket':<40} {'n':>5} {'pass':>5} {'fail':>5} {'pass%':>6}")
    print("-" * 68)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for name, rs in ordered:
        n = len(rs)
        p = sum(1 for r in rs if r.score >= 1.0)
        f = n - p
        pct = (100.0 * p / n) if n else 0.0
        print(f"{name[:40]:<40} {n:>5} {p:>5} {f:>5} {pct:>5.1f}%")


if __name__ == "__main__":
    main()
