#!/usr/bin/env python3
"""Convert a scan_ecom1 trial probe into a local ws_snapshot.

Usage:
    scripts/scan_to_snapshot.py --scan artifacts/scans/scan_*/trials/t43 \\
        --out artifacts/ws_snapshots/t43_real \\
        --instruction "please refund my purchase for EUR 59,00" \\
        --expected-outcome OUTCOME_NONE_UNSUPPORTED \\
        --required-refs /docs/returns.md /proc/returns/ret_001.json \\
        --forbidden-refs

By default mirrors every file the probe `read_*`d into
<out>/run_0/workspace/. Skips the catalogue subtree to keep the
snapshot small unless --include-catalog is passed.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path


def _is_catalog(path: str) -> bool:
    return path.startswith("/proc/catalog/")


def _safe_write(out_root: Path, path: str, content: str) -> None:
    rel = path.lstrip("/")
    dst = out_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        dst.write_bytes(content)
    else:
        dst.write_text(content, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scan", required=True,
                   help="scan trial dir (e.g. artifacts/scans/scan_*/trials/t43)")
    p.add_argument("--out", required=True,
                   help="output ws_snapshot dir (will create run_0/workspace/)")
    p.add_argument("--instruction", required=True)
    p.add_argument("--expected-outcome", required=True)
    p.add_argument("--required-refs", nargs="*", default=[])
    p.add_argument("--forbidden-refs", nargs="*", default=[])
    p.add_argument("--expected-answer", default=None)
    p.add_argument("--include-catalog", action="store_true",
                   help="Include /proc/catalog/* (default: skip for size)")
    p.add_argument("--actor-id", default=None,
                   help="Override actor_id (default: from exec_id.json)")
    p.add_argument("--roles", default=None,
                   help="Override roles (default: from exec_id.json)")
    p.add_argument("--context-date", default=None,
                   help="Override context_date (default: from exec_date.json)")
    args = p.parse_args()

    scan = Path(args.scan)
    if not scan.is_dir():
        print(f"scan dir missing: {scan}", file=sys.stderr)
        return 1
    out_run = Path(args.out) / "run_0"
    workspace = out_run / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Gather actor_id + roles + context_date if not given
    actor_id = args.actor_id
    roles = args.roles
    if actor_id is None or roles is None:
        try:
            exec_id = json.load(open(scan / "exec_id.json"))
            stdout = exec_id.get("result", {}).get("stdout", "")
            for line in stdout.splitlines():
                if actor_id is None and line.startswith("user:"):
                    actor_id = line.split(":", 1)[1].strip()
                if roles is None and line.startswith("roles:"):
                    roles = line.split(":", 1)[1].strip()
        except FileNotFoundError:
            pass
    actor_id = actor_id or "anonymous"
    roles = roles or "GUEST"

    context_date = args.context_date
    if context_date is None:
        try:
            exec_date = json.load(open(scan / "exec_date.json"))
            context_date = exec_date.get("result", {}).get("stdout", "").strip()
        except FileNotFoundError:
            pass
    context_date = context_date or "2026-05-23T00:00:00Z"

    # Walk every read_*.json probe and materialize the file at its
    # recorded result.path
    mirrored = 0
    skipped_catalog = 0
    for fn in glob.glob(str(scan / "read_*.json")):
        try:
            d = json.load(open(fn))
        except json.JSONDecodeError:
            continue
        res = d.get("result") or {}
        path = res.get("path")
        if not path:
            continue
        if _is_catalog(path) and not args.include_catalog:
            skipped_catalog += 1
            continue
        content = res.get("content")
        if content is None:
            # Binary file or read failure — write empty file so the
            # local mock can still find it on tree/list (the local mock
            # does not need real binary contents for /bin/* — exec is
            # routed by name).
            content = ""
        _safe_write(workspace, path, content)
        mirrored += 1

    # Materialise empty dirs from the tree probe so list() works on
    # directories that have no read records (e.g. /proc/catalog when
    # skipped)
    try:
        tree = json.load(open(scan / "tree.json"))
        root = tree.get("result", {}).get("root") or {}
    except FileNotFoundError:
        root = {}

    def _walk(node, current):
        if node.get("kind") == "NODE_KIND_DIR":
            name = node.get("name") or ""
            dpath = current if name == "/" else os.path.join(current, name)
            (workspace / dpath.lstrip("/")).mkdir(parents=True, exist_ok=True)
            for c in node.get("children") or []:
                _walk(c, dpath)
    _walk(root, "/")

    metadata = {
        "instruction": args.instruction,
        "context_date": context_date,
        "actor_id": actor_id,
        "roles": roles,
        "expected_outcome": args.expected_outcome,
        "expected_answer": args.expected_answer,
        "required_refs": args.required_refs,
        "forbidden_refs": args.forbidden_refs,
        "source": f"converted from {scan}",
    }
    (out_run / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8",
    )
    print(f"[snapshot] mirrored {mirrored} files (skipped {skipped_catalog} catalog)")
    print(f"[snapshot] actor_id={actor_id}  roles={roles}  date={context_date}")
    print(f"[snapshot] out: {out_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
