# src/bitgn_scraper/probe_cli.py
"""Phase 2 CLI shim. Iterates task_instantiations and probes each."""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitgn_scraper probe")
    p.add_argument("--benchmark-id", default="bitgn/pac1-prod")
    p.add_argument("--task-ids", default="",
                   help="comma-separated subset; empty = all instantiations in DB")
    p.add_argument("--db-path", type=Path,
                   default=Path("artifacts/harness_db/bitgn_local.db"))
    p.add_argument("--p2b-sample", type=int, default=0,
                   help="number of instantiations to additionally hit with P2b mutation probe")
    p.add_argument("--p6-sample", type=int, default=0,
                   help="number of instantiations to additionally hit with P6 random probe")
    p.add_argument("--seed", type=int, default=0,
                   help="seed for diagnostic-sample selection")
    return p


def run_probe_cli() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.clients import build_harness_client, build_pcm_client
    from bitgn_scraper.probes import probe_instantiation

    harness = build_harness_client()

    rows = _load_instantiations(args.db_path, args.task_ids)
    if not rows:
        print("[probe] no instantiations in DB — run `scrape` first", flush=True)
        return 1

    rng = random.Random(args.seed)
    p2b_set = set(rng.sample(range(len(rows)), min(args.p2b_sample, len(rows))))
    p6_set = set(rng.sample(range(len(rows)), min(args.p6_sample, len(rows))))

    print(f"[probe] {len(rows)} instantiation(s) → {args.db_path}", flush=True)
    total = 0
    for i, (task_id, inst_hash) in enumerate(rows):
        print(f"[probe] ({i + 1}/{len(rows)}) {task_id} {inst_hash[:12]}", flush=True)
        n = probe_instantiation(
            harness_client=harness,
            pcm_factory=build_pcm_client,
            task_id=task_id,
            benchmark_id=args.benchmark_id,
            instruction_hash=inst_hash,
            known_rules={},
            db_path=args.db_path,
            run_diagnostic_p2b=(i in p2b_set),
            run_diagnostic_p6=(i in p6_set),
        )
        total += n
        print(f"  → {n} probe(s)", flush=True)

    print(f"[probe] done — {total} probe(s) total", flush=True)
    return 0


def _load_instantiations(db_path: Path, task_filter: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as cx:
        if task_filter:
            tids = [t.strip() for t in task_filter.split(",") if t.strip()]
            placeholders = ",".join("?" * len(tids))
            sql = (
                "SELECT task_id, instantiation_hash FROM task_instantiations "
                f"WHERE task_id IN ({placeholders}) ORDER BY task_id, instantiation_hash"
            )
            return list(cx.execute(sql, tids).fetchall())
        return list(cx.execute(
            "SELECT task_id, instantiation_hash FROM task_instantiations "
            "ORDER BY task_id, instantiation_hash"
        ).fetchall())
