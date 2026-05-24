#!/usr/bin/env python3
"""Build per-(task, lang) ws_snapshots from a canonical English snapshot
and the translation table.

Each new snapshot symlinks the workspace from the canonical EN snapshot
(disk-cheap) and writes its own metadata.json with the translated
instruction. Required-refs / forbidden-refs / expected_outcome stay
identical to EN (the grader is language-neutral on paths + enums).

Usage:
    scripts/build_i18n_snapshots.py \\
        --translations artifacts/i18n/translations.json \\
        --canonical-map t11:artifacts/ws_snapshots/t11_real,... \\
        --out-root artifacts/ws_snapshots
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--translations", required=True)
    p.add_argument("--canonical-map", required=True,
                   help="task_id:canonical_dir,task_id:canonical_dir,...")
    p.add_argument("--out-root", default="artifacts/ws_snapshots")
    args = p.parse_args()

    tx = json.load(open(args.translations, encoding="utf-8"))
    canon = {}
    for entry in args.canonical_map.split(","):
        k, v = entry.split(":", 1)
        canon[k.strip()] = Path(v.strip()).resolve()

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    built = 0
    for tid, entry in tx.items():
        if tid not in canon:
            print(f"  [{tid}] no canonical; skip")
            continue
        src = canon[tid]
        en_meta = json.load(open(src / "run_0" / "metadata.json"))
        en_workspace = src / "run_0" / "workspace"
        if not en_workspace.is_dir():
            print(f"  [{tid}] canonical workspace missing at {en_workspace}")
            continue
        for lang, payload in entry["translations"].items():
            snap_dir = out_root / f"{tid}_{lang}"
            run_dir = snap_dir / "run_0"
            run_dir.mkdir(parents=True, exist_ok=True)
            ws_link = run_dir / "workspace"
            # Remove any stale link/dir, recreate
            if ws_link.is_symlink() or ws_link.exists():
                if ws_link.is_symlink():
                    ws_link.unlink()
                elif ws_link.is_dir():
                    shutil.rmtree(ws_link)
                else:
                    ws_link.unlink()
            # Use relative symlink so the snapshot is portable across worktrees
            rel = os.path.relpath(en_workspace, run_dir)
            ws_link.symlink_to(rel)

            meta = dict(en_meta)
            meta["instruction"] = payload["text"]
            meta["instruction_language"] = lang
            meta["source"] = (
                f"i18n translation of {tid} EN→{lang}; "
                f"workspace symlinked from {en_workspace}"
            )
            meta["i18n_preserved_tokens"] = payload.get("preserved", [])
            (run_dir / "metadata.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8",
            )
            built += 1
            print(f"  [{tid}/{lang}] built {snap_dir}")
    print(f"[done] built {built} snapshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
