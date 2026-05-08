"""Pick eight canonical sentinels from ingested baselines.

**Plan deviation (2026-04-11):** the writing-plans draft assumed a
per-task `category` field on the server's `BenchmarkTask` proto (one
of knowledge/relationship/finance/document/inbox/communication/
security/exception-handling). Probing GetBenchmark and GetTrial at
runtime shows neither carries a category — the BenchmarkTask proto is
`{task_id, preview, hint}` and Trial is `{trial_id, instruction, ...,
score, score_detail, state, ...}`. The category taxonomy exists only
in the spec-text/dashboard UI, not on the wire.

Selection strategy (score-diversity instead):
  1. Gather every task's aggregated bitgn_score across baselines and
     its set of score_detail strings.
  2. Rank failing tasks (mean score < 1.0) by a composite of
     (failure_mass, detail_richness, detail_uniqueness).
  3. Greedy-pick eight tasks, rejecting any pick whose score_detail
     set overlaps a previously-picked task's detail set (so every
     sentinel covers a distinct failure mode).
  4. If fewer than eight failing-and-distinct tasks exist, backfill
     with the next-highest-failure-mass tasks regardless of overlap.

Runs once at M0; output is committed to
docs/superpowers/specs/sentinels.csv and maintained by hand after that.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


_TARGET_COUNT = 8


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("bench_files", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=Path("docs/superpowers/specs/sentinels.csv"))
    args = p.parse_args()

    # task_id -> aggregated record
    tasks: dict[str, dict] = {}
    for bp in args.bench_files:
        data = json.loads(bp.read_text())
        for task_id, task_entry in data.get("tasks", {}).items():
            entry = tasks.setdefault(task_id, {
                "task_id": task_id,
                "scores": [],
                "score_details": set(),
                "instruction": task_entry.get("bitgn_instruction", ""),
                "last_outcomes": set(),
            })
            s = task_entry.get("bitgn_score")
            if s is not None:
                entry["scores"].append(float(s))
            detail_list = task_entry.get("bitgn_score_detail") or []
            for d in detail_list:
                if d:
                    entry["score_details"].add(d.strip())
            outcome = task_entry.get("last_outcome")
            if outcome:
                entry["last_outcomes"].add(outcome)
            if not entry["instruction"]:
                entry["instruction"] = task_entry.get("bitgn_instruction") or ""

    ranked: list[dict] = []
    for t in tasks.values():
        scores = t["scores"] or [1.0]
        mean = sum(scores) / len(scores)
        failure_mass = sum(1.0 - s for s in scores)
        ranked.append({
            **t,
            "mean": mean,
            "failure_mass": failure_mass,
            "detail_count": len(t["score_details"]),
        })

    # Sort: failing first (highest failure_mass), then detail-richness,
    # then task_id for stable order.
    ranked.sort(
        key=lambda t: (-t["failure_mass"], -t["detail_count"], t["task_id"]),
    )

    picks: list[dict] = []
    covered_details: set[str] = set()
    for t in ranked:
        if t["failure_mass"] <= 0.0:
            continue  # all-pass tasks have zero diagnostic value
        overlap = t["score_details"] & covered_details
        if picks and overlap:
            continue  # we already have a sentinel covering this failure mode
        picks.append(t)
        covered_details |= t["score_details"]
        if len(picks) >= _TARGET_COUNT:
            break

    # Backfill with whatever failing tasks remain if we didn't reach eight.
    if len(picks) < _TARGET_COUNT:
        already = {p["task_id"] for p in picks}
        for t in ranked:
            if t["failure_mass"] <= 0.0:
                continue
            if t["task_id"] in already:
                continue
            picks.append(t)
            if len(picks) >= _TARGET_COUNT:
                break

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "task_id",
                "baseline_mean_score",
                "failure_mass",
                "distinct_detail_count",
                "justification",
            ],
        )
        w.writeheader()
        for t in picks:
            w.writerow({
                "task_id": t["task_id"],
                "baseline_mean_score": f"{t['mean']:.3f}",
                "failure_mass": f"{t['failure_mass']:.3f}",
                "distinct_detail_count": t["detail_count"],
                "justification": (
                    " | ".join(sorted(t["score_details"]))[:400]
                    if t["score_details"]
                    else "(no score_detail)"
                ),
            })
    print(f"wrote {len(picks)} sentinels to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
