#!/usr/bin/env python3
"""Fetch per-trial score_detail + grader log lines for evaluated trials.

Read-only (get_trial) — does not start trials or count against the run
rate limit. After a run is EVALUATED, score_detail typically carries the
grader's verdict (expected vs got), which is what we need to root-cause
content-layer failures offline.

Usage:
    .venv/bin/python scripts/fetch_trial_detail.py <trial_id> [<trial_id> ...]
"""
from __future__ import annotations

import os
import sys

from bitgn_contest_agent.harness import BitgnHarness


def main() -> int:
    trial_ids = sys.argv[1:]
    if not trial_ids:
        print("usage: fetch_trial_detail.py <trial_id> ...", file=sys.stderr)
        return 2
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set (source .env)", file=sys.stderr)
        return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")
    harness = BitgnHarness.from_env(
        benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key
    )
    for tid in trial_ids:
        resp = harness.get_trial(tid)
        print("=" * 70)
        print(f"trial={tid} task={resp.task_id} score={resp.score} "
              f"available={resp.score_available} state={resp.state}")
        if resp.error:
            print("ERROR:", resp.error[:300])
        print("--- score_detail ---")
        print((resp.score_detail or "(empty)")[:2000])
        # grader-relevant log lines
        grader_lines = [
            ln.text for ln in resp.logs
            if ln.text and any(k in ln.text.lower()
                               for k in ("grad", "expect", "score", "answer", "ref"))
        ]
        if grader_lines:
            print("--- grader log lines ---")
            for ln in grader_lines[:25]:
                print(ln[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
