"""Compare two `scrape_prod_full.py` runs for PROD determinism.

Reads `<run_dir>/trials/<task_id>.json` from each run, then reports:
  - per task_id: structure + content match (sha256 set per workspace)
  - per task_id: whether the instruction string matched
  - per instruction: whether the same instruction maps to the same workspace
    (different runs may rotate the task_id behind a given instruction)

Usage:
    scripts/compare_scrape_runs.py <run1_dir> <run2_dir> [--out report.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_run(run_dir: Path) -> dict[str, dict]:
    trials_dir = run_dir / "trials"
    out: dict[str, dict] = {}
    for jf in sorted(trials_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        out[data["task_id"]] = data
    return out


def _file_sigs(trial: dict) -> dict[str, tuple[str, int]]:
    return {f["path"]: (f["sha256"], f["byte_size"])
            for f in trial.get("workspace_files", [])}


def main() -> int:
    parser = argparse.ArgumentParser(prog="compare_scrape_runs")
    parser.add_argument("run1", type=Path)
    parser.add_argument("run2", type=Path)
    parser.add_argument("--out", type=Path, default=None,
                        help="write JSON report here (default: stdout summary only)")
    args = parser.parse_args()

    a = _load_run(args.run1)
    b = _load_run(args.run2)

    common_ids = sorted(set(a) & set(b))
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))

    by_task: list[dict] = []
    instr_match_count = 0
    ws_match_count = 0
    ws_mismatch: list[dict] = []
    for tid in common_ids:
        ta, tb = a[tid], b[tid]
        sa, sb = _file_sigs(ta), _file_sigs(tb)
        instr_eq = ta.get("instruction") == tb.get("instruction")
        ws_eq = sa == sb
        if instr_eq:
            instr_match_count += 1
        if ws_eq:
            ws_match_count += 1
        else:
            paths_a, paths_b = set(sa), set(sb)
            missing = sorted(paths_a - paths_b)
            extra = sorted(paths_b - paths_a)
            content_diffs = sorted(p for p in (paths_a & paths_b) if sa[p] != sb[p])
            ws_mismatch.append({
                "task_id": tid,
                "missing_in_b": missing,
                "extra_in_b": extra,
                "content_diff_paths": content_diffs,
                "file_count_a": len(sa),
                "file_count_b": len(sb),
            })
        by_task.append({
            "task_id": tid,
            "instruction_match": instr_eq,
            "workspace_match": ws_eq,
            "instruction_a": ta.get("instruction", "")[:120],
            "instruction_b": tb.get("instruction", "")[:120],
        })

    by_instruction: list[dict] = []
    inst_to_a = {t["instruction"]: t for t in a.values() if t.get("instruction")}
    inst_to_b = {t["instruction"]: t for t in b.values() if t.get("instruction")}
    common_inst = sorted(set(inst_to_a) & set(inst_to_b))
    inst_ws_match_count = 0
    for inst in common_inst:
        ta, tb = inst_to_a[inst], inst_to_b[inst]
        sa, sb = _file_sigs(ta), _file_sigs(tb)
        ws_eq = sa == sb
        if ws_eq:
            inst_ws_match_count += 1
        by_instruction.append({
            "instruction": inst[:120],
            "task_id_a": ta["task_id"],
            "task_id_b": tb["task_id"],
            "workspace_match": ws_eq,
        })

    report = {
        "run_a": str(args.run1),
        "run_b": str(args.run2),
        "trials_a": len(a),
        "trials_b": len(b),
        "common_task_ids": len(common_ids),
        "task_ids_only_in_a": only_a,
        "task_ids_only_in_b": only_b,
        "by_task_id": {
            "instruction_match": instr_match_count,
            "workspace_match": ws_match_count,
            "workspace_mismatch_detail": ws_mismatch,
        },
        "by_instruction": {
            "common_instructions": len(common_inst),
            "workspace_match": inst_ws_match_count,
        },
        "samples": by_task[:5],
    }

    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True))

    n = len(common_ids)
    print(f"trials: a={len(a)} b={len(b)} common={n}")
    print(f"  by task_id  : instruction_match={instr_match_count}/{n} "
          f"workspace_match={ws_match_count}/{n}")
    print(f"  by instruct : common={len(common_inst)} "
          f"workspace_match={inst_ws_match_count}/{len(common_inst)}")
    if ws_mismatch:
        print(f"  workspace mismatches ({len(ws_mismatch)}):")
        for m in ws_mismatch[:5]:
            print(f"    {m['task_id']}: files {m['file_count_a']} vs "
                  f"{m['file_count_b']} miss={len(m['missing_in_b'])} "
                  f"extra={len(m['extra_in_b'])} "
                  f"diff={len(m['content_diff_paths'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
