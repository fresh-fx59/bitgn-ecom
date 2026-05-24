#!/usr/bin/env python3
"""Cross-language A/B: run the agent on EN canonical + each translated
variant, compare outcomes / refs / pass-rate. Outputs a matrix that
shows the "language gap" introduced by enforcer regex matching.

Usage:
    scripts/i18n_ab.py \\
        --tasks t11,t13,t21,t28,t40,t43 \\
        --langs en,de,cs,hu,ja \\
        --runs 1 \\
        --out artifacts/i18n/ab_<ts>.json
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _run_one(snapshot_dir: Path, runs: int, log_dir: Path) -> dict:
    """Run local_bench.py on one snapshot, return parsed results."""
    cmd = [
        sys.executable, "scripts/local_bench.py",
        "--snapshot", str(snapshot_dir),
        "--runs", str(runs),
        "--log-dir", str(log_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    out_lines = proc.stdout.splitlines()
    # local_bench prints lines like:
    #   [PASS] t43_real#0  outcome=OUTCOME_NONE_UNSUPPORTED  steps=9 wall=72.2s detail=...
    rows = []
    for ln in out_lines:
        if "outcome=" not in ln:
            continue
        ok = "[PASS]" in ln
        parts = ln.strip().split()
        outcome = next((p.split("=",1)[1] for p in parts if p.startswith("outcome=")), "?")
        steps = next((p.split("=",1)[1] for p in parts if p.startswith("steps=")), "?")
        wall = next((p.split("=",1)[1] for p in parts if p.startswith("wall=")), "?")
        # everything after "detail=" until end of line
        if "detail=" in ln:
            detail = ln.split("detail=",1)[1].strip()
        else:
            detail = ""
        rows.append({
            "ok": ok,
            "outcome": outcome,
            "steps": steps,
            "wall": wall,
            "detail": detail,
        })
    return {
        "snapshot": str(snapshot_dir),
        "exit_code": proc.returncode,
        "rows": rows,
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-10:]),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", required=True)
    p.add_argument("--langs", required=True)
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--out", default=None)
    p.add_argument("--root", default="artifacts/ws_snapshots")
    args = p.parse_args()

    tasks = [t.strip() for t in args.tasks.split(",")]
    langs = [l.strip() for l in args.langs.split(",")]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out or f"artifacts/i18n/ab_{ts}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_root = Path(f"logs/i18n_ab_{ts}")
    log_root.mkdir(parents=True, exist_ok=True)
    root = Path(args.root)

    results: dict[str, dict[str, dict]] = {}
    for tid in tasks:
        results[tid] = {}
        for lang in langs:
            if lang == "en":
                snap = root / f"{tid}_en"
                if not snap.is_dir():
                    snap = root / f"{tid}_real"
            else:
                snap = root / f"{tid}_{lang}"
            if not snap.is_dir():
                print(f"  [{tid}/{lang}] snapshot missing at {snap}")
                results[tid][lang] = {"error": f"missing {snap}"}
                continue
            log_dir = log_root / f"{tid}_{lang}"
            print(f"  [{tid}/{lang}] running… ({snap})", flush=True)
            r = _run_one(snap, args.runs, log_dir)
            results[tid][lang] = r
            pass_count = sum(1 for row in r["rows"] if row["ok"])
            total = len(r["rows"])
            outcomes = ",".join(sorted(set(row["outcome"] for row in r["rows"])))
            print(f"     {pass_count}/{total}  outcomes={outcomes}")

    summary = {
        "ts": ts,
        "tasks": tasks,
        "langs": langs,
        "runs_per_cell": args.runs,
        "results": results,
    }
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n[done] {out_path}")

    # Print compact matrix
    print("\n=== Pass-rate matrix")
    header = f"{'task':<6}" + "".join(f"{l:>10}" for l in langs)
    print(header)
    for tid in tasks:
        row = f"{tid:<6}"
        for lang in langs:
            r = results[tid].get(lang, {})
            if "error" in r:
                row += f"{'(none)':>10}"
            else:
                pc = sum(1 for x in r["rows"] if x["ok"])
                tc = len(r["rows"])
                row += f"{f'{pc}/{tc}':>10}"
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
