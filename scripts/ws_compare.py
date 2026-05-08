#!/usr/bin/env -S python3 -u
"""Compare two workspace snapshots and report differences.

Can compare:
  - Two runs of the same task (determinism check)
  - Two different tasks (understand workspace variation)
  - Any two snapshot directories

Usage:
    # Compare two runs of same task
    python scripts/ws_compare.py artifacts/ws_snapshots/t055/run_0 \
                                  artifacts/ws_snapshots/t055/run_1

    # Show full content diff for changed files
    python scripts/ws_compare.py --show-diff \
        artifacts/ws_snapshots/t055/run_0 artifacts/ws_snapshots/t055/run_1
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Dict, Set


def load_snapshot(run_dir: Path) -> tuple[dict, Dict[str, str]]:
    """Load meta.json and all file contents from a snapshot directory."""
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        print(f"ERROR: {meta_path} not found", file=sys.stderr)
        sys.exit(1)

    meta = json.loads(meta_path.read_text())
    workspace = run_dir / "workspace"

    files: Dict[str, str] = {}
    if workspace.exists():
        for fpath in sorted(workspace.rglob("*")):
            if fpath.is_file():
                rel = "/" + str(fpath.relative_to(workspace))
                try:
                    files[rel] = fpath.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    files[rel] = f"<binary: {fpath.stat().st_size} bytes>"

    return meta, files


def compare(dir_a: Path, dir_b: Path, show_diff: bool = False) -> dict:
    """Compare two workspace snapshots. Returns a report dict."""
    meta_a, files_a = load_snapshot(dir_a)
    meta_b, files_b = load_snapshot(dir_b)

    paths_a: Set[str] = set(files_a.keys())
    paths_b: Set[str] = set(files_b.keys())

    only_a = sorted(paths_a - paths_b)
    only_b = sorted(paths_b - paths_a)
    common = sorted(paths_a & paths_b)

    identical: list[str] = []
    changed: list[str] = []
    diffs: Dict[str, str] = {}

    for p in common:
        if files_a[p] == files_b[p]:
            identical.append(p)
        else:
            changed.append(p)
            if show_diff:
                diff = difflib.unified_diff(
                    files_a[p].splitlines(keepends=True),
                    files_b[p].splitlines(keepends=True),
                    fromfile=f"A:{p}",
                    tofile=f"B:{p}",
                    lineterm="",
                )
                diffs[p] = "\n".join(diff)

    report = {
        "dir_a": str(dir_a),
        "dir_b": str(dir_b),
        "task_a": meta_a.get("task_id", "?"),
        "task_b": meta_b.get("task_id", "?"),
        "instruction_a": meta_a.get("instruction", "")[:200],
        "instruction_b": meta_b.get("instruction", "")[:200],
        "instructions_match": meta_a.get("instruction") == meta_b.get("instruction"),
        "files_a": len(paths_a),
        "files_b": len(paths_b),
        "only_in_a": only_a,
        "only_in_b": only_b,
        "identical": len(identical),
        "changed": changed,
    }

    return report, diffs


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two workspace snapshots")
    parser.add_argument("dir_a", type=Path, help="First snapshot directory")
    parser.add_argument("dir_b", type=Path, help="Second snapshot directory")
    parser.add_argument("--show-diff", action="store_true",
                        help="Show unified diff for changed files")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    report, diffs = compare(args.dir_a, args.dir_b, show_diff=args.show_diff)

    if args.json_out:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # Human-readable output
    print(f"Comparing:")
    print(f"  A: {report['dir_a']} ({report['task_a']}, {report['files_a']} files)")
    print(f"  B: {report['dir_b']} ({report['task_b']}, {report['files_b']} files)")
    print()

    inst_match = report["instructions_match"]
    print(f"Instructions match: {inst_match}")
    if not inst_match:
        print(f"  A: {report['instruction_a'][:100]}...")
        print(f"  B: {report['instruction_b'][:100]}...")
    print()

    if report["only_in_a"]:
        print(f"Only in A ({len(report['only_in_a'])} files):")
        for p in report["only_in_a"]:
            print(f"  - {p}")
        print()

    if report["only_in_b"]:
        print(f"Only in B ({len(report['only_in_b'])} files):")
        for p in report["only_in_b"]:
            print(f"  + {p}")
        print()

    print(f"Common files: {report['identical'] + len(report['changed'])}")
    print(f"  Identical: {report['identical']}")
    print(f"  Changed:   {len(report['changed'])}")

    if report["changed"]:
        print()
        print("Changed files:")
        for p in report["changed"]:
            print(f"  ~ {p}")
            if p in diffs:
                print()
                for line in diffs[p].split("\n")[:50]:
                    print(f"    {line}")
                print()

    # Summary
    total_diffs = len(report["only_in_a"]) + len(report["only_in_b"]) + len(report["changed"])
    if total_diffs == 0:
        print("\n✓ Workspaces are identical")
    else:
        print(f"\n✗ {total_diffs} differences found")


if __name__ == "__main__":
    main()
