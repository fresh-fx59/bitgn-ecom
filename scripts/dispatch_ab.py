#!/usr/bin/env python3
"""A/B a candidate dispatch planner vs the current greedy, scored by the local
simulator across a PARAMETER SWEEP (robustness). A candidate ships only if it
dominates on the model-free metrics (more on-time, ≤ transport, higher gain)
across the whole sweep — not a single tuning.

Reports, per wave fixture and per sim regime: on_time, late, transport, gain
for {current, candidate}. The grader's own gain = money_made - transport -
late - missed, so on_time↑ / late↓ / transport↓ ⇒ grader gain↑ regardless of
the exact (unknown) reference-optimal.
"""
from __future__ import annotations

import os
import sys

from bitgn_contest_agent import dispatch_planner as dp
from scripts.dispatch_sim import SimParams, score_plan


WAVES = [
    "artifacts/prod_explore/dispatch_wave_iWNjqLmp",
    "artifacts/ws_snapshots/prod_run1/t014_prod_r1/run_0/workspace/ops/dispatch/wave-ApdgNmry",
    "artifacts/ws_snapshots/prod_run1/t024_prod_r1/run_0/workspace/ops/dispatch/wave-BD2bv3HB",
    "artifacts/ws_snapshots/prod_run1/t044_prod_r1/run_0/workspace/ops/dispatch/wave-Yj4oo8jz",
]

# sim regimes — grader-calibrated (late penalty ~8 EUR/time per the t004
# score_detail) + an explicit margin-forfeit axis, since the grader's
# efficiency gap (17%, ~110 EUR) far exceeds the 6 EUR per-time penalty,
# implying lateness ALSO forfeits margin. We vary delay severity AND the
# forfeit fraction so a shipped change must win across the whole grid.
REGIMES = [
    ("pen-only/lo",  SimParams(p_unlikely=0.12, p_likely=0.50, late_penalty_per_time=8,  late_margin_forfeit=0.0)),
    ("pen-only/hi",  SimParams(p_unlikely=0.25, p_likely=0.70, late_penalty_per_time=8,  late_margin_forfeit=0.0)),
    ("forfeit0.3",   SimParams(p_unlikely=0.18, p_likely=0.62, late_penalty_per_time=8,  late_margin_forfeit=0.3)),
    ("forfeit0.6",   SimParams(p_unlikely=0.25, p_likely=0.70, late_penalty_per_time=8,  late_margin_forfeit=0.6)),
]


def _load(wave_dir):
    pk = open(os.path.join(wave_dir, "packages.tsv"), encoding="utf-8").read()
    ln = open(os.path.join(wave_dir, "lanes.tsv"), encoding="utf-8").read()
    return dp.parse_packages(pk), dp.parse_lanes(ln)


def run(candidate_fn=None, label="candidate"):
    for wave in WAVES:
        if not os.path.isdir(wave):
            print(f"  (skip missing {wave})")
            continue
        pkgs, lanes = _load(wave)
        cur = dp.plan(pkgs, lanes)
        cand = candidate_fn(pkgs, lanes) if candidate_fn else None
        valid_cand = cand is not None and dp.validate(cand, pkgs, lanes)
        name = os.path.basename(wave.rstrip("/")) or wave
        print(f"\n=== {name}  ({len(pkgs)} pkgs) ===")
        for rlabel, params in REGIMES:
            params.n_trials = 5000
            rc = score_plan(cur, pkgs, lanes, params)
            line = (f"  [{rlabel:7}] current : on_time={rc['on_time']:.2f} "
                    f"late={rc['late']:.2f} transport={rc['transport']:.1f} "
                    f"gain={rc['gain']:.1f}")
            print(line)
            if valid_cand:
                ra = score_plan(cand, pkgs, lanes, params)
                d_on = ra['on_time'] - rc['on_time']
                d_gain = ra['gain'] - rc['gain']
                flag = "  <== WIN" if d_gain > 0.5 else ("  (~)" if abs(d_gain) <= 0.5 else "  <<< REGRESS")
                print(f"            {label}: on_time={ra['on_time']:.2f} "
                      f"late={ra['late']:.2f} transport={ra['transport']:.1f} "
                      f"gain={ra['gain']:.1f}  d_gain={d_gain:+.1f}{flag}")
            elif candidate_fn:
                print(f"            {label}: INVALID PLAN (abstain)")


if __name__ == "__main__":
    # default: compare current planner vs the experimental plan_v2 if present
    cand = getattr(dp, "plan_v2", None)
    run(cand if callable(cand) else None, label="plan_v2")
