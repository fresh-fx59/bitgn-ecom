#!/usr/bin/env python3
"""A/B a set of BITGN_USE_* levers against a hand-picked snapshot list.

Runs each snapshot N times under a BASELINE env and a CANDIDATE env
(the prod-emulation flags are applied to BOTH so the only delta is the
levers under test), then prints a side-by-side pass-rate table and flags
any per-snapshot regression (candidate rate < baseline rate).

This is the §6 "How to A/B" pattern from the ecom1prod precision plan,
generalised so the disjoint snapshot names don't need a common glob.

Usage:
    scripts/ab_levers.py --runs 3 \
        --snapshots t01_real2 t16_real2 inj_trusted_override \
        --candidate BITGN_USE_JQ=1 BITGN_USE_SKU_NUDGE=1 \
                    BITGN_USE_DISPATCH_PLANNER=1
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

# reuse the local_bench machinery verbatim
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_bench as lb  # noqa: E402

# prod-emulation flags applied to BOTH arms (isolate the levers under test)
_PROD_EMU = {"BITGN_LOCAL_SQL_UNAVAILABLE": "1", "BITGN_LOCAL_PROD_PATHS": "1"}

# lever flags that must be cleared in the baseline arm so it is truly the
# v0.1.152 behavior regardless of ambient env
_LEVER_KEYS = (
    "BITGN_USE_JQ", "BITGN_USE_SKU_NUDGE", "BITGN_USE_DISPATCH_PLANNER",
    "BITGN_USE_REDERIVE_COUNT", "BITGN_USE_REFLESS_COUNT_OVERRIDE",
)


def _apply_env(overrides: dict[str, str]) -> None:
    for k in _LEVER_KEYS:
        os.environ.pop(k, None)
    for k, v in _PROD_EMU.items():
        os.environ[k] = v
    for k, v in overrides.items():
        os.environ[k] = v


def _run_arm(snaps, runs, backend, log_dir, label, overrides, workers):
    """Run all (snapshot x run) trials of one arm concurrently. Env is set
    once before the pool starts and is fixed for the whole arm, so the
    process-global os.environ the agent reads has no race. A barrier
    (pool join) separates arms so the candidate env never overlaps the
    baseline env."""
    from concurrent.futures import ThreadPoolExecutor
    _apply_env(overrides)
    jobs = [(snap, i) for snap in snaps for i in range(runs)]
    passed_by: dict[str, int] = {s.name: 0 for s in snaps}

    def _do(job):
        snap, i = job
        return snap, lb._run_one(
            snap=snap, run_index=i, backend=backend,
            log_dir=log_dir / label, max_steps=int(os.environ.get("MAX_STEPS", "40")),
            llm_http_timeout_sec=float(os.environ.get("LLM_HTTP_TIMEOUT_SEC", "120")),
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for snap, r in pool.map(_do, jobs):
            mark = "PASS" if r.passed else ("WARN" if r.detail.startswith("UNGRADED") else "FAIL")
            print(f"  [{label}][{mark}] {snap.name}#{r.run_index} outcome={r.outcome} "
                  f"steps={r.steps} {r.wall_sec:.0f}s :: {r.detail[:110]}", flush=True)
            if r.passed and not r.detail.startswith("UNGRADED"):
                passed_by[snap.name] += 1
    return {name: passed_by[name] / runs for name in passed_by}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--snapshots", nargs="+", required=True)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--candidate", nargs="+", required=True,
                   help="KEY=VALUE lever overrides for the candidate arm")
    p.add_argument("--root", type=Path, default=Path("artifacts/ws_snapshots"))
    p.add_argument("--log-dir", type=Path, default=Path("logs/ab_levers"))
    p.add_argument("--workers", type=int, default=6,
                   help="concurrent trials per arm (shared backend)")
    args = p.parse_args()

    cand = {}
    for kv in args.candidate:
        k, _, v = kv.partition("=")
        cand[k] = v or "1"

    snaps = [lb.Snapshot.load(args.root / n) for n in args.snapshots]
    import time
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_dir = args.log_dir / ts
    backend = lb._build_backend()
    print(f"# A/B {len(snaps)} snapshot(s) x {args.runs} runs | candidate={cand}")
    print(f"# model={os.environ.get('AGENT_MODEL','?')} effort={os.environ.get('AGENT_REASONING_EFFORT','medium')}")

    base = _run_arm(snaps, args.runs, backend, log_dir, "baseline", {}, args.workers)
    cand_rates = _run_arm(snaps, args.runs, backend, log_dir, "candidate", cand, args.workers)

    print("\n# RESULTS  (baseline -> candidate)")
    regressions = []
    for snap in snaps:
        b, c = base[snap.name], cand_rates[snap.name]
        flag = ""
        if c < b:
            flag = "  <-- REGRESSION"
            regressions.append(snap.name)
        elif c > b:
            flag = "  (+lift)"
        print(f"  {snap.name:30} {b*100:5.0f}% -> {c*100:5.0f}%{flag}")
    print(f"\n# baseline mean {sum(base.values())/len(base)*100:.1f}%  "
          f"candidate mean {sum(cand_rates.values())/len(cand_rates)*100:.1f}%")
    if regressions:
        print(f"# !!! {len(regressions)} REGRESSION(S): {regressions}")
        return 1
    print("# no regressions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
