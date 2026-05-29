#!/usr/bin/env python3
"""Fetch authoritative per-trial scores for a finished (evaluated) BitGN run.

Under the 2026-05 harness change, `end_trial` no longer returns a score
(`score_available=False`); scores are released in a batch only after
`submit_run` and server-side evaluation. This reads them back via
`get_run` (a read-only RPC — it neither starts trials nor burns VM
wall-clock, so it does NOT count against the run rate limit).

Usage:
    .venv/bin/python scripts/fetch_run_scores.py <run_id> [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from bitgn_contest_agent.harness import BitgnHarness


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--benchmark", default=os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev"))
    ap.add_argument("--json", default=None, help="write per-task scores to this path")
    args = ap.parse_args()

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set (source .env first)", file=sys.stderr)
        return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"

    harness = BitgnHarness.from_env(
        benchmark=args.benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key
    )
    resp = harness.get_run(args.run_id)

    rows = []
    for th in resp.trials:
        rows.append(
            {
                "task_id": th.task_id,
                "trial_id": th.trial_id,
                "num": th.num,
                "score": float(th.score) if th.score_available else None,
                "score_available": bool(th.score_available),
                "state": int(th.state),
                "error": th.error,
            }
        )
    rows.sort(key=lambda r: r["task_id"])

    passed = sum(1 for r in rows if (r["score"] or 0.0) >= 1.0)
    total = len(rows)
    print(f"run_id={args.run_id} state={resp.state} "
          f"overall_score={resp.score:.4f} available={resp.score_available}")
    print(f"pass(1.0)={passed}/{total}")
    print("--- failing / partial ---")
    for r in rows:
        sc = r["score"]
        if sc is None or sc < 1.0:
            print(f"  {r['task_id']:>5} score={sc} state={r['state']} {r['error'][:60]}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(
                {"run_id": args.run_id, "state": int(resp.state),
                 "overall_score": float(resp.score), "tasks": rows},
                f, indent=2,
            )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
