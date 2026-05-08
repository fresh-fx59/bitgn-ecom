"""Fetch PROD run data from BitGN server and produce intent-based report.

Accepts dashboard URLs or run IDs directly. Fetches trial-level data
(instruction, score, score_detail) via the BitGN API and runs the intent
report without needing pre-existing bench JSON files.

Usage:
    # From dashboard URLs
    uv run python scripts/fetch_intent_report.py \
        https://eu.bitgn.com/runs/run-22J17Q9aR8GNVLw5EBvCuyc4e

    # From run IDs
    uv run python scripts/fetch_intent_report.py \
        run-22J17Q9aR8GNVLw5EBvCuyc4e

    # Compare two runs
    uv run python scripts/fetch_intent_report.py \
        --baseline run-22HrXikys1AXh4Fy6vQckdKXX \
        run-22J17Q9aR8GNVLw5EBvCuyc4e

    # Filter to specific intents
    uv run python scripts/fetch_intent_report.py \
        --intent receipt_total_relative --intent inbox_en \
        run-22J17Q9aR8GNVLw5EBvCuyc4e

    # Save fetched data as bench-compatible JSON
    uv run python scripts/fetch_intent_report.py \
        --save artifacts/bench/fetched_run.json \
        run-22J17Q9aR8GNVLw5EBvCuyc4e

    # JSON output
    uv run python scripts/fetch_intent_report.py --json <run-id>

Env:
    BITGN_API_KEY    required
    BITGN_BASE_URL   optional, default https://api.bitgn.com
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import intent_report for the reporting logic
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from intent_report import (
    INTENT_POSITIONS,
    POSITION_TO_INTENT,
    build_intent_report,
    build_json_output,
    print_report,
)


# ---------------------------------------------------------------------------
# BitGN API client (reuses pattern from ingest_bitgn_scores.py)
# ---------------------------------------------------------------------------

def _make_client():
    from bitgn.harness_connect import HarnessServiceClientSync
    from bitgn.harness_pb2 import RunKind, RunState  # noqa: F401
    from connectrpc.interceptor import MetadataInterceptorSync

    class _AuthInterceptor(MetadataInterceptorSync):
        def __init__(self, api_key: str) -> None:
            self._api_key = api_key

        def on_start_sync(self, ctx: Any) -> None:
            ctx.request_headers()["authorization"] = f"Bearer {self._api_key}"
            return None

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        raise SystemExit("BITGN_API_KEY is not set")
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com").rstrip("/")
    return HarnessServiceClientSync(base, interceptors=(_AuthInterceptor(api_key),))


def _enum_name(enum_cls: Any, value: int) -> str:
    try:
        return enum_cls.Name(value)
    except Exception:
        return str(value)


def _extract_run_id(url_or_id: str) -> str:
    """Extract run ID from a dashboard URL or return as-is if already an ID."""
    # Match: https://eu.bitgn.com/runs/run-XXXX or just run-XXXX
    m = re.search(r"(run-[A-Za-z0-9]+)", url_or_id)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot extract run ID from: {url_or_id!r}")


def fetch_run_as_bench(run_id: str) -> dict:
    """Fetch a run from BitGN and return data shaped like a bench summary JSON.

    This produces a structure compatible with intent_report.build_intent_report().
    """
    from bitgn.harness_pb2 import GetRunRequest, GetTrialRequest, RunKind, RunState

    client = _make_client()
    run = client.get_run(GetRunRequest(run_id=run_id))

    print(f"  Fetching {len(run.trials)} trials for {run_id}...", file=sys.stderr)

    tasks: dict[str, dict] = {}
    for i, head in enumerate(run.trials, 1):
        detail = client.get_trial(GetTrialRequest(trial_id=head.trial_id))
        tasks[head.task_id] = {
            "runs": 1,
            "passes": 1 if float(detail.score) >= 0.999 else 0,
            "bitgn_trial_id": detail.trial_id,
            "bitgn_score": float(detail.score),
            "bitgn_state": _enum_name(RunState, detail.state),
            "bitgn_error": detail.error or "",
            "bitgn_score_detail": list(detail.score_detail),
            "bitgn_instruction": detail.instruction,
            "last_outcome": detail.error or "OUTCOME_OK",
            "step_texts": [],
            "median_steps": 0,
            "category": "other",
        }
        if i % 20 == 0 or i == len(run.trials):
            print(f"  fetched {i}/{len(run.trials)} trials", file=sys.stderr)
        time.sleep(0.05)

    server_total = sum(float(t.score) for t in run.trials)
    bench = {
        "schema_version": "1.1.0",
        "bitgn": {
            "run_id": run.run_id,
            "run_name": run.name,
            "benchmark_id": run.benchmark_id,
            "state": _enum_name(RunState, run.state),
            "kind": _enum_name(RunKind, run.kind),
            "server_score_mean": float(run.score),
            "server_score_total": server_total,
            "total_trials": len(run.trials),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "overall": {
            "total_runs": len(run.trials),
            "total_passes": sum(
                1 for t in tasks.values()
                if t.get("bitgn_score", 0) >= 0.999
            ),
            "pass_rate": float(run.score),
        },
        "tasks": tasks,
    }

    ok = bench["overall"]["total_passes"]
    total = len(run.trials)
    print(
        f"  {run_id}: {ok}/{total} tasks passed "
        f"(server_score_total={server_total:.1f})",
        file=sys.stderr,
    )
    return bench


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "run",
        help="Dashboard URL or run ID (e.g. run-22J17Q9aR8GNVLw5EBvCuyc4e)",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="Previous run URL/ID to compare against",
    )
    p.add_argument(
        "--save",
        type=Path,
        default=None,
        help="Save fetched data as bench-compatible JSON",
    )
    p.add_argument("--failures-only", action="store_true")
    p.add_argument("--intent", action="append", dest="intents", default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    # Fetch main run
    run_id = _extract_run_id(args.run)
    bench = fetch_run_as_bench(run_id)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        with args.save.open("w") as f:
            json.dump(bench, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  Saved to {args.save}", file=sys.stderr)

    report = build_intent_report(bench)

    # Filter intents
    if args.intents:
        unknown = set(args.intents) - set(report.keys())
        if unknown:
            print(f"Warning: unknown intents: {unknown}", file=sys.stderr)
        report = {k: v for k, v in report.items() if k in args.intents}

    # Baseline
    baseline_report = None
    if args.baseline:
        baseline_id = _extract_run_id(args.baseline)
        baseline_bench = fetch_run_as_bench(baseline_id)
        baseline_report = build_intent_report(baseline_bench)

    if args.json:
        output = build_json_output(report, run_id, baseline_report)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print_report(
            report,
            failures_only=args.failures_only,
            baseline_report=baseline_report,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
