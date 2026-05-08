"""One-task PROD probe — what server-side feedback is available?

Resolves spec Open Question 1. Walks one task through the playground
flow and prints every server-side field we can see: StartPlayground
response, EndTrial immediate score, and GetTrial post-hoc score +
score_detail + error + logs.

Does NOT run the agent — submits nothing, just calls end_trial on the
freshly-started trial. This is a lightweight probe, not a real trial.
It consumes one playground slot per invocation.

Usage:
    set -a && source /home/claude-developer/bitgn-contest/.env && set +a
    uv run python scripts/verify_prod_grader.py --task-id t001

Env:
    BITGN_API_KEY    required
    BITGN_BASE_URL   optional, default https://api.bitgn.com
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetTrialRequest,
    StartPlaygroundRequest,
    TrialState,
)
from connectrpc.interceptor import MetadataInterceptorSync


class _Auth(MetadataInterceptorSync):
    def __init__(self, key: str) -> None:
        self._key = key

    def on_start_sync(self, ctx: Any) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._key}"
        return None


def _enum(cls: Any, n: int) -> str:
    try:
        return cls.Name(n)
    except Exception:
        return str(n)


def _dump(label: str, obj: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(obj, indent=2, default=str))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-id", default="t001")
    p.add_argument("--benchmark", default="bitgn/pac1-prod")
    args = p.parse_args()

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        raise SystemExit("BITGN_API_KEY is not set")
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com").rstrip("/")
    client = HarnessServiceClientSync(base, interceptors=(_Auth(api_key),))

    print(f"probing {args.task_id} on {args.benchmark}...", file=sys.stderr)

    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id=args.benchmark, task_id=args.task_id)
    )
    _dump("StartPlaygroundResponse", {
        "trial_id": started.trial_id,
        "task_id": started.task_id,
        "benchmark_id": started.benchmark_id,
        "instruction": started.instruction,
        "harness_url": started.harness_url,
    })

    # Immediate end_trial with no agent work — see whether score is
    # embargoed (returns 0 and must wait for run-level eval) or open
    # (returns a real grader score immediately).
    ended = client.end_trial(EndTrialRequest(trial_id=started.trial_id))
    _dump("EndTrialResponse (immediate)", {
        "trial_id": ended.trial_id,
        "state": _enum(TrialState, ended.state),
        "score": float(ended.score),
        "score_detail": list(ended.score_detail),
    })

    # A small delay, then GetTrial to see post-hoc shape including logs
    time.sleep(0.5)
    detail = client.get_trial(GetTrialRequest(trial_id=started.trial_id))
    _dump("GetTrialResponse", {
        "trial_id": detail.trial_id,
        "task_id": detail.task_id,
        "benchmark_id": detail.benchmark_id,
        "run_id": detail.run_id,
        "state": _enum(TrialState, detail.state),
        "score": float(detail.score),
        "score_detail": list(detail.score_detail),
        "error": detail.error,
        "instruction": detail.instruction,
        "next_cursor": detail.next_cursor,
        "log_count": len(detail.logs),
        "log_sample_first5": [
            {
                "time": l.time,
                "unix_ms": l.unix_ms,
                "kind": l.kind,
                "type": l.type,
                "text": (l.text or "")[:200],
            }
            for l in detail.logs[:5]
        ],
    })

    return 0


if __name__ == "__main__":
    sys.exit(main())
