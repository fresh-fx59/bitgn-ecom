# src/bitgn_scraper/scrape_cli.py
"""Phase 1 CLI shim. Lists tasks via GetBenchmark, scrapes each."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitgn_scraper scrape")
    p.add_argument("--benchmark-id", default="bitgn/pac1-prod")
    p.add_argument("--task-ids", default="",
                   help="comma-separated subset; empty = all tasks from GetBenchmark")
    p.add_argument("--max-attempts", type=int, default=30)
    p.add_argument("--saturation-threshold", type=int, default=5)
    p.add_argument("--db-path", type=Path,
                   default=Path("artifacts/harness_db/bitgn_local.db"))
    p.add_argument("--workspace-root", type=Path,
                   default=Path("artifacts/harness_db/workspaces"))
    return p


def run_scrape_cli() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.clients import build_harness_client, build_pcm_client
    from bitgn_scraper.db import init_schema
    from bitgn_scraper.scrape_runner import scrape_task

    init_schema(args.db_path)
    harness = build_harness_client()
    task_ids = _resolve_task_ids(harness, args.benchmark_id, args.task_ids)

    print(f"[scrape] {len(task_ids)} task(s) → {args.db_path}", flush=True)
    total_new = 0
    for i, tid in enumerate(task_ids, 1):
        print(f"[scrape] ({i}/{len(task_ids)}) {tid}", flush=True)
        n = scrape_task(
            harness_client=harness,
            pcm_factory=build_pcm_client,
            task_id=tid,
            benchmark_id=args.benchmark_id,
            db_path=args.db_path,
            workspace_root=args.workspace_root,
            max_attempts=args.max_attempts,
            saturation_threshold=args.saturation_threshold,
        )
        total_new += n
        print(f"  → {n} new instantiation(s)", flush=True)

    print(f"[scrape] done — {total_new} new instantiation(s) total", flush=True)
    return 0


def _resolve_task_ids(harness: Any, benchmark_id: str, override: str) -> list[str]:
    if override:
        return [t.strip() for t in override.split(",") if t.strip()]
    from bitgn.harness_pb2 import GetBenchmarkRequest
    resp = harness.get_benchmark(GetBenchmarkRequest(benchmark_id=benchmark_id))
    return list(resp.task_ids)
