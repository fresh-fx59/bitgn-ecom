"""Compare a freshly-ingested M0-gate bench against the committed baseline.

Reads two bench JSONs (post-`ingest_bitgn_scores.py`) and prints:
  - server_score_total delta
  - per-outcome histogram diff
  - per-task shifts (which tasks moved from OK → FAIL and vice-versa)
  - go/no-go verdict per the plan (delta >= -2.0 → PASS)

Usage:
    uv run python scripts/m0_gate_compare.py \\
        --baseline artifacts/bench/2e6f621_hints1_p16i24_gpt54_20260411T132438Z_prod_runs1.json \\
        --new      artifacts/bench/aab6675_m0gate_p16i24_gpt54_20260411T223213Z_prod_runs1.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _totals(bench: dict) -> tuple[float, int, Counter]:
    total = 0.0
    ok = 0
    histogram: Counter = Counter()
    for tid, t in bench.get("tasks", {}).items():
        score = float(t.get("bitgn_score", 0.0))
        total += score
        if score >= 0.999:
            ok += 1
        err = t.get("bitgn_error") or "OUTCOME_OK"
        histogram[err] += 1
    return total, ok, histogram


def _task_set(bench: dict, predicate) -> set[str]:
    return {tid for tid, t in bench.get("tasks", {}).items() if predicate(t)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--new", required=True, type=Path)
    p.add_argument("--fail-threshold", type=float, default=-2.0,
                   help="delta below this is a GATE FAIL (default -2.0, per plan)")
    args = p.parse_args()

    baseline = _load(args.baseline)
    new = _load(args.new)

    b_total, b_ok, b_hist = _totals(baseline)
    n_total, n_ok, n_hist = _totals(new)

    print("=" * 72)
    print("M0 GATE COMPARISON")
    print("=" * 72)
    print(f"baseline: {args.baseline.name}")
    print(f"new:      {args.new.name}")
    print()
    print(f"server_score_total:   baseline={b_total:6.2f}   new={n_total:6.2f}   delta={n_total - b_total:+6.2f}")
    print(f"ok_count (>=0.999):   baseline={b_ok:3d}      new={n_ok:3d}      delta={n_ok - b_ok:+3d}")
    print()

    print("OUTCOME HISTOGRAM:")
    print(f"  {'outcome':<38} {'baseline':>10} {'new':>10} {'delta':>10}")
    all_outcomes = sorted(set(b_hist) | set(n_hist))
    for o in all_outcomes:
        bh = b_hist.get(o, 0)
        nh = n_hist.get(o, 0)
        print(f"  {o:<38} {bh:>10} {nh:>10} {nh - bh:>+10}")
    print()

    ok_thresh = lambda t: float(t.get("bitgn_score", 0.0)) >= 0.999
    fail_thresh = lambda t: float(t.get("bitgn_score", 0.0)) < 0.999

    b_ok_set = _task_set(baseline, ok_thresh)
    b_fail_set = _task_set(baseline, fail_thresh)
    n_ok_set = _task_set(new, ok_thresh)
    n_fail_set = _task_set(new, fail_thresh)

    cleared = sorted(b_fail_set & n_ok_set)  # baseline FAIL -> new OK
    regressed = sorted(b_ok_set & n_fail_set)  # baseline OK -> new FAIL
    persistent_fails = sorted(b_fail_set & n_fail_set)

    print(f"CLEARED (baseline FAIL → new OK, {len(cleared)} tasks):")
    for tid in cleared:
        print(f"  +{tid}")
    print()
    print(f"REGRESSED (baseline OK → new FAIL, {len(regressed)} tasks):")
    for tid in regressed:
        t = new["tasks"][tid]
        detail = (t.get("bitgn_score_detail") or [""])[0][:120]
        print(f"  -{tid}: score={t.get('bitgn_score', 0):.2f} detail={detail!r}")
    print()
    print(f"PERSISTENT FAILS (both baseline and new, {len(persistent_fails)} tasks):")
    print(f"  {', '.join(persistent_fails)}")
    print()

    # Verdict
    delta = n_total - b_total
    print("=" * 72)
    if delta >= args.fail_threshold:
        print(f"VERDICT: PASS (delta {delta:+.2f} >= threshold {args.fail_threshold:+.2f})")
        print("=" * 72)
        return 0
    else:
        print(f"VERDICT: FAIL (delta {delta:+.2f} < threshold {args.fail_threshold:+.2f})")
        print("Consider rolling back task 0.4 (base-prompt restructure)")
        print("=" * 72)
        return 1


if __name__ == "__main__":
    sys.exit(main())
