"""Augment a PROD bench summary JSON with server-side scores from BitGN.

PROD has no local grader, so landing-day bench summaries are produced with
`passes: 0` everywhere — only the outcome histogram is a validation signal
at write time. Once BitGN has finished scoring a run, `GetRun`/`GetTrial`
expose the per-trial float score (0..1), any trial error, and a
`score_detail` list of strings that explains partial/zero credit.

This script fetches those scores and writes them back into the existing
bench summary file as *additive* `bitgn_*` fields, preserving the original
outcome-histogram view untouched.

Usage:

    uv run python scripts/ingest_bitgn_scores.py \
        --run-id run-22HmyCmzpTRhamwo395k7bj6m \
        --bench artifacts/bench/52f4e03_fix3_sonnet46_20260411T111525Z_prod_runs1.json

Multiple --run-id/--bench pairs may be passed; they pair by position.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import GetRunRequest, GetTrialRequest, RunKind, RunState
from connectrpc.interceptor import MetadataInterceptorSync


class _AuthHeaderInterceptor(MetadataInterceptorSync):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def on_start_sync(self, ctx: Any) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._api_key}"
        return None


def _client() -> HarnessServiceClientSync:
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        raise SystemExit("BITGN_API_KEY is not set")
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com").rstrip("/")
    return HarnessServiceClientSync(base, interceptors=(_AuthHeaderInterceptor(api_key),))


def _enum_name(enum_cls: Any, value: int) -> str:
    try:
        return enum_cls.Name(value)
    except Exception:
        return str(value)


def fetch_run(client: HarnessServiceClientSync, run_id: str) -> dict[str, Any]:
    """Return a dict mapping task_id -> {trial_id, score, error, state,
    instruction, score_detail} plus a top-level `_run` entry with run-level
    metadata.

    Each trial is fetched via GetTrial to obtain instruction + score_detail,
    which are not carried on the TrialHead objects returned by GetRun.
    """
    run = client.get_run(GetRunRequest(run_id=run_id))
    out: dict[str, Any] = {
        "_run": {
            "run_id": run.run_id,
            "run_name": run.name,
            "benchmark_id": run.benchmark_id,
            "state": _enum_name(RunState, run.state),
            "kind": _enum_name(RunKind, run.kind),
            "server_score_mean": float(run.score),
            "server_score_total": sum(float(t.score) for t in run.trials),
            "total_trials": len(run.trials),
            "stats_done_count": int(run.stats.done_count),
            "stats_error_count": int(run.stats.error_count),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    }
    for i, head in enumerate(run.trials, 1):
        detail = client.get_trial(GetTrialRequest(trial_id=head.trial_id))
        out[head.task_id] = {
            "bitgn_trial_id": detail.trial_id,
            "bitgn_score": float(detail.score),
            "bitgn_state": _enum_name(RunState, detail.state),
            "bitgn_error": detail.error or "",
            "bitgn_score_detail": list(detail.score_detail),
            "bitgn_instruction": detail.instruction,
        }
        if i % 10 == 0 or i == len(run.trials):
            print(f"  fetched {i}/{len(run.trials)} trials", file=sys.stderr)
        # Be polite to the server
        time.sleep(0.05)
    return out


def augment_bench_file(bench_path: Path, fetched: dict[str, Any]) -> None:
    if not bench_path.exists():
        raise SystemExit(f"bench file not found: {bench_path}")
    with bench_path.open() as fh:
        data = json.load(fh)
    data["bitgn"] = fetched["_run"]
    tasks = data.get("tasks", {})
    missing = []
    for task_id, task_entry in tasks.items():
        trial = fetched.get(task_id)
        if trial is None:
            missing.append(task_id)
            continue
        task_entry.update(trial)
    if missing:
        print(f"  WARNING: no BitGN data for tasks: {missing}", file=sys.stderr)
    with bench_path.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        action="append",
        required=True,
        help="BitGN run id (repeatable, paired positionally with --bench)",
    )
    parser.add_argument(
        "--bench",
        action="append",
        required=True,
        type=Path,
        help="Path to bench summary JSON (repeatable, paired positionally with --run-id)",
    )
    args = parser.parse_args()
    if len(args.run_id) != len(args.bench):
        raise SystemExit("--run-id and --bench must be given in equal numbers")

    client = _client()
    for run_id, bench_path in zip(args.run_id, args.bench):
        print(f"== {run_id} -> {bench_path}", file=sys.stderr)
        fetched = fetch_run(client, run_id)
        server_score = fetched["_run"]["server_score_total"]
        total = fetched["_run"]["total_trials"]
        print(
            f"  server score: {server_score:.2f} / {total} "
            f"(mean={fetched['_run']['server_score_mean']:.4f})",
            file=sys.stderr,
        )
        augment_bench_file(bench_path, fetched)
        print(f"  wrote {bench_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
