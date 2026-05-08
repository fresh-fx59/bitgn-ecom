# src/bitgn_scraper/validate_cli.py
"""Phase 3 CLI shim. Runs integrity + coverage + (optional) determinism."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bitgn_scraper validate")
    p.add_argument("--db-path", type=Path,
                   default=Path("artifacts/harness_db/bitgn_local.db"))
    p.add_argument("--workspace-root", type=Path,
                   default=Path("artifacts/harness_db/workspaces"))
    p.add_argument("--skip-determinism", action="store_true",
                   help="skip the live re-scrape determinism check (no PROD calls)")
    p.add_argument("--determinism-task-ids", default="t001,t010,t020,t030,t050",
                   help="comma-separated tasks to re-scrape for determinism check")
    p.add_argument("--determinism-samples", type=int, default=5)
    p.add_argument("--out-root", type=Path,
                   default=Path("artifacts/harness_db/scrape_runs"))
    return p


def run_validate_cli() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:])

    from bitgn_scraper.validate import (
        check_probe_coverage,
        check_workspace_integrity,
    )

    print(f"[validate] integrity check on {args.db_path}", flush=True)
    integrity = check_workspace_integrity(args.db_path, args.workspace_root)
    print(f"  → checked={integrity.checked} mismatches={len(integrity.mismatches)} "
          f"missing={len(integrity.missing_files)}", flush=True)

    print(f"[validate] coverage stats", flush=True)
    coverage = check_probe_coverage(args.db_path)
    print(f"  → total={coverage.total_tasks} high={coverage.tasks_with_high_confidence} "
          f"low_only={coverage.tasks_with_low_confidence_only} "
          f"no_rules={coverage.tasks_with_no_rules}", flush=True)

    out: dict = {
        "ran_at": datetime.now(tz=timezone.utc).isoformat(),
        "integrity": _integrity_to_dict(integrity),
        "coverage": asdict(coverage),
    }

    if not args.skip_determinism:
        print(f"[validate] determinism re-scrape on {args.determinism_task_ids}", flush=True)
        from bitgn_scraper.clients import build_harness_client, build_pcm_client
        from bitgn_scraper.validate import check_determinism

        harness = build_harness_client()
        tids = [t.strip() for t in args.determinism_task_ids.split(",") if t.strip()]
        det = check_determinism(
            db_path=args.db_path,
            harness_client=harness,
            pcm_factory=build_pcm_client,
            task_ids=tids,
            n_samples_per_task=args.determinism_samples,
        )
        for f in det.findings:
            print(f"  → {f.task_id}: stored={len(f.stored_hashes)} "
                  f"new_distinct={len(set(f.new_hashes))} overlap={f.overlap} "
                  f"saturated={f.fully_saturated}", flush=True)
        out["determinism"] = {
            "findings": [asdict(f) for f in det.findings],
        }

    out_dir = args.out_root / datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "validate_report.json"
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str))
    print(f"[validate] wrote {out_path}", flush=True)
    return 0


def _integrity_to_dict(report) -> dict:
    return {
        "checked": report.checked,
        "mismatches": [
            {"task_id": m.task_id, "instantiation_hash": m.instantiation_hash,
             "path": m.path, "expected_sha256": m.expected_sha256,
             "actual_sha256": m.actual_sha256}
            for m in report.mismatches
        ],
        "missing_files": [
            {"task_id": t, "instantiation_hash": i, "path": p}
            for t, i, p in report.missing_files
        ],
    }
