#!/usr/bin/env python3
"""Compare N enum scans of the contest surface to map per-task
variance (which fields differ trial-to-trial).

Usage:
    scripts/diff_enums.py artifacts/enum/enum_<ts1> artifacts/enum/enum_<ts2> ...

For every task_id present across the inputs:
  - if all instructions match byte-identical → STABLE
  - else → VARIABLE: print a unified-style diff of the variants

The contest's task templates are stable across trial seeds, but the
fillable slots (basket IDs, customer emails, amounts, store names,
attribute specs) can differ per trial. Knowing which task families
are HIGH-variance tells us where seed-variance failures are most
likely and where the agent's robustness matters most.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("dirs", nargs="+", help="enum dirs (each contains trials/<task>.json)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print the differing instructions side-by-side")
    args = p.parse_args()

    enums: list[dict[str, str]] = []
    for d in args.dirs:
        root = Path(d)
        trials_dir = root / "trials"
        if not trials_dir.is_dir():
            print(f"  WARN: no trials/ under {d}", file=sys.stderr)
            continue
        by_task = {}
        for trial in sorted(trials_dir.glob("t*.json")):
            data = json.load(open(trial))
            by_task[data["task_id"]] = data["instruction"]
        enums.append(by_task)

    if not enums:
        print("no enums", file=sys.stderr)
        return 1

    # Union of all task_ids
    all_tasks = sorted({tid for e in enums for tid in e.keys()})

    stable = []
    variable = []
    new_in_some = []
    for tid in all_tasks:
        variants = [e.get(tid) for e in enums]
        if any(v is None for v in variants):
            new_in_some.append(tid)
            continue
        if len(set(variants)) == 1:
            stable.append(tid)
        else:
            variable.append(tid)

    print(f"compared {len(enums)} enum scans across {len(all_tasks)} unique task_ids")
    print(f"  stable (byte-identical across all scans): {len(stable)}")
    print(f"  variable (differs at least one scan):     {len(variable)}")
    print(f"  not present in all scans:                  {len(new_in_some)}")
    print()
    if variable:
        print("=== VARIABLE tasks (seed-dependent template fills):")
        for tid in variable:
            print(f"\n--- {tid}")
            for i, e in enumerate(enums):
                instr = e.get(tid, "(missing)")
                if args.verbose:
                    print(f"  [{i}] {instr}")
                else:
                    print(f"  [{i}] {instr[:140]}")
    if new_in_some:
        print(f"\n=== Tasks NOT present in all scans (added/removed?):")
        for tid in new_in_some:
            present = [i for i, e in enumerate(enums) if tid in e]
            print(f"  {tid}: in scans {present}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
