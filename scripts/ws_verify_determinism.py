#!/usr/bin/env -S python3 -u
"""Verify workspace determinism: same task → same filesystem?

Runs ws_dump N times for the same task(s) and compares the snapshots.
Reports whether file structure and content are identical across runs,
and if not, exactly which files differ.

Usage:
    # Dump 3 runs and verify
    python scripts/ws_verify_determinism.py --task-id t055 --runs 3

    # Verify from existing dumps (skip re-dumping)
    python scripts/ws_verify_determinism.py --task-id t055 --runs 3 --skip-dump

    # Multiple tasks
    python scripts/ws_verify_determinism.py --task-id t055 t034 t076 --runs 2
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


def load_meta(run_dir: Path) -> dict:
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No meta.json in {run_dir}")
    return json.loads(meta_path.read_text())


def compare_runs(task_id: str, snapshot_dir: Path, run_count: int) -> dict:
    """Compare N runs of the same task. Returns a report dict."""
    runs: List[Tuple[int, dict]] = []
    for i in range(run_count):
        run_dir = snapshot_dir / task_id / f"run_{i}"
        if not run_dir.exists():
            return {"task_id": task_id, "error": f"run_{i} not found at {run_dir}"}
        meta = load_meta(run_dir)
        runs.append((i, meta))

    if len(runs) < 2:
        return {"task_id": task_id, "error": "need at least 2 runs to compare"}

    base_idx, base_meta = runs[0]
    base_hashes: Dict[str, str] = base_meta["file_hashes"]
    base_files: Set[str] = set(base_hashes.keys())

    report = {
        "task_id": task_id,
        "runs_compared": len(runs),
        "base_file_count": len(base_files),
        "instructions_identical": True,
        "structure_identical": True,
        "content_identical": True,
        "differing_instructions": [],
        "missing_in_runs": {},
        "extra_in_runs": {},
        "content_diffs": {},
    }

    base_instruction = base_meta["instruction"]

    for idx, meta in runs[1:]:
        run_label = f"run_{idx}"

        # Check instruction
        if meta["instruction"] != base_instruction:
            report["instructions_identical"] = False
            report["differing_instructions"].append({
                "run": run_label,
                "instruction": meta["instruction"][:200],
            })

        # Check file structure
        run_hashes: Dict[str, str] = meta["file_hashes"]
        run_files: Set[str] = set(run_hashes.keys())

        missing = base_files - run_files
        extra = run_files - base_files

        if missing:
            report["structure_identical"] = False
            report["missing_in_runs"][run_label] = sorted(missing)
        if extra:
            report["structure_identical"] = False
            report["extra_in_runs"][run_label] = sorted(extra)

        # Check content (for files present in both)
        common = base_files & run_files
        for fpath in sorted(common):
            if base_hashes[fpath] != run_hashes[fpath]:
                report["content_identical"] = False
                if fpath not in report["content_diffs"]:
                    report["content_diffs"][fpath] = []
                report["content_diffs"][fpath].append({
                    "run": run_label,
                    "base_sha256": base_hashes[fpath][:16],
                    "this_sha256": run_hashes[fpath][:16],
                })

    return report


def print_report(report: dict) -> None:
    task_id = report["task_id"]
    if "error" in report:
        print(f"  {task_id}: ERROR — {report['error']}")
        return

    runs = report["runs_compared"]
    files = report["base_file_count"]
    inst_ok = report["instructions_identical"]
    struct_ok = report["structure_identical"]
    content_ok = report["content_identical"]

    status = "DETERMINISTIC" if (struct_ok and content_ok) else "NON-DETERMINISTIC"
    print(f"  {task_id}: {status} ({runs} runs, {files} files)")
    print(f"    Instructions identical: {inst_ok}")
    print(f"    Structure identical:    {struct_ok}")
    print(f"    Content identical:      {content_ok}")

    if not inst_ok:
        print(f"    ⚠ Instructions differ across runs (expected — PROD reshuffles prompts)")
        for diff in report["differing_instructions"]:
            print(f"      {diff['run']}: {diff['instruction'][:100]}...")

    if not struct_ok:
        for run, missing in report.get("missing_in_runs", {}).items():
            print(f"    ✗ {run} missing {len(missing)} files: {missing[:5]}")
        for run, extra in report.get("extra_in_runs", {}).items():
            print(f"    ✗ {run} has {len(extra)} extra files: {extra[:5]}")

    if not content_ok:
        diffs = report["content_diffs"]
        print(f"    ✗ {len(diffs)} files with content differences:")
        for fpath, changes in list(diffs.items())[:10]:
            print(f"      {fpath}")
            for c in changes:
                print(f"        {c['run']}: {c['base_sha256']}… → {c['this_sha256']}…")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify workspace determinism across runs")
    parser.add_argument("--task-id", nargs="+", required=True)
    parser.add_argument("--runs", type=int, default=3, help="Number of runs to compare")
    parser.add_argument("--snapshot-dir", default="artifacts/ws_snapshots")
    parser.add_argument("--skip-dump", action="store_true",
                        help="Skip dumping — assume snapshots already exist")
    parser.add_argument("--env-file", default=".worktrees/plan-b/.env")
    parser.add_argument("--benchmark", default=None)
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir)

    # Step 1: dump if needed
    if not args.skip_dump:
        dump_cmd = [
            sys.executable, "scripts/ws_dump.py",
            "--task-id", *args.task_id,
            "--runs", str(args.runs),
            "--output", str(snapshot_dir),
            "--env-file", args.env_file,
        ]
        if args.benchmark:
            dump_cmd.extend(["--benchmark", args.benchmark])

        print(f"Dumping {args.runs} runs for {len(args.task_id)} tasks...\n")
        result = subprocess.run(dump_cmd, cwd=Path(__file__).resolve().parent.parent)
        if result.returncode != 0:
            print("ERROR: ws_dump.py failed", file=sys.stderr)
            sys.exit(1)
        print()

    # Step 2: compare
    print("=" * 60)
    print("DETERMINISM REPORT")
    print("=" * 60)

    all_deterministic = True
    reports = []
    for task_id in args.task_id:
        report = compare_runs(task_id, snapshot_dir, args.runs)
        reports.append(report)
        print_report(report)
        if not (report.get("structure_identical", False) and
                report.get("content_identical", False)):
            if "error" not in report:
                all_deterministic = False
        print()

    # Save combined report
    report_path = snapshot_dir / "determinism_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(reports, indent=2, ensure_ascii=False))
    print(f"Report saved to {report_path}")

    if all_deterministic:
        print("\n✓ All tasks are workspace-deterministic (instructions may differ)")
    else:
        print("\n✗ Some tasks have non-deterministic workspaces")
        sys.exit(1)


if __name__ == "__main__":
    main()
