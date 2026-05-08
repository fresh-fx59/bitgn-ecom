#!/usr/bin/env -S python3 -u
"""Dump a complete workspace snapshot from a BitGN task via the playground flow.

Connects to PROD, starts a playground trial for the given task, walks the
full filesystem tree via PCM tree()+read(), and saves every file to a local
directory.  No LLM calls — pure PCM ops.

Usage:
    # Single task
    python scripts/ws_dump.py --task-id t055

    # Multiple tasks
    python scripts/ws_dump.py --task-id t055 t034 t076

    # Custom output dir / benchmark
    python scripts/ws_dump.py --task-id t055 --output artifacts/ws_snapshots \
        --benchmark bitgn/pac1-prod

Env vars (auto-loaded from .worktrees/plan-b/.env if present):
    BITGN_API_KEY       — required
    BITGN_BASE_URL      — defaults to https://api.bitgn.com
    BITGN_BENCHMARK     — defaults to bitgn/pac1-prod
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bitgn.vm import pcm_pb2
from bitgn_contest_agent.harness import BitgnHarness, StartedTask


@dataclass
class FileEntry:
    path: str
    content: str
    sha256: str
    size: int


@dataclass
class WorkspaceSnapshot:
    task_id: str
    instruction: str
    trial_id: str
    timestamp: str
    files: Dict[str, FileEntry] = field(default_factory=dict)
    tree_text: str = ""


def _load_env(env_path: str = ".worktrees/plan-b/.env") -> None:
    """Load key=value pairs from env file if it exists (no shell expansion)."""
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _flatten_tree(entry: pcm_pb2.TreeResponse.Entry, prefix: str = "") -> List[str]:
    """Recursively flatten a TreeResponse.Entry into a list of file paths."""
    paths: List[str] = []
    # Root node typically has name="/" — normalize to empty prefix
    name = entry.name
    if name == "/":
        name = ""
    current = f"{prefix}/{name}" if name else prefix
    if not entry.is_dir:
        if current:
            paths.append(current)
    else:
        for child in entry.children:
            child_paths = _flatten_tree(child, current)
            paths.extend(child_paths)
    return paths


def _tree_to_text(entry: pcm_pb2.TreeResponse.Entry, indent: int = 0) -> str:
    """Render tree as indented text for human inspection."""
    lines: List[str] = []
    prefix = "  " * indent
    name = entry.name or "/"
    if entry.is_dir:
        lines.append(f"{prefix}{name}/")
        for child in sorted(entry.children, key=lambda e: (not e.is_dir, e.name)):
            lines.append(_tree_to_text(child, indent + 1))
    else:
        lines.append(f"{prefix}{name}")
    return "\n".join(lines)


def dump_workspace(
    harness: BitgnHarness,
    task_id: str,
    output_dir: Path,
    run_index: int = 0,
) -> WorkspaceSnapshot:
    """Start a playground trial, dump the full workspace, return snapshot."""
    print(f"  [{task_id}] Starting playground trial...")
    started = harness.start_task(task_id)
    print(f"  [{task_id}] Trial {started.trial_id} started")
    print(f"  [{task_id}] Instruction: {started.instruction[:120]}...")

    runtime = started.runtime_client

    # 1. Get full tree
    print(f"  [{task_id}] Fetching tree...")
    tree_resp = runtime.tree(pcm_pb2.TreeRequest(root="/"))
    file_paths = _flatten_tree(tree_resp.root)
    tree_text = _tree_to_text(tree_resp.root)
    print(f"  [{task_id}] Found {len(file_paths)} files")

    # 2. Read every file
    snapshot = WorkspaceSnapshot(
        task_id=task_id,
        instruction=started.instruction,
        trial_id=started.trial_id,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        tree_text=tree_text,
    )

    for i, fpath in enumerate(file_paths):
        try:
            resp = runtime.read(pcm_pb2.ReadRequest(path=fpath))
            content = resp.content
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            snapshot.files[fpath] = FileEntry(
                path=fpath,
                content=content,
                sha256=sha,
                size=len(content.encode("utf-8")),
            )
        except Exception as exc:
            print(f"  [{task_id}] WARN: failed to read {fpath}: {exc}")

    print(f"  [{task_id}] Read {len(snapshot.files)}/{len(file_paths)} files")

    # 3. End trial (cleanup — no answer submitted)
    try:
        score, detail = harness.end_task(started)
        print(f"  [{task_id}] Trial ended (score={score}, detail_count={len(detail)})")
    except Exception as exc:
        print(f"  [{task_id}] WARN: end_task failed: {exc}")

    # 4. Save to disk
    run_dir = output_dir / task_id / f"run_{run_index}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save files preserving directory structure
    for fpath, entry in snapshot.files.items():
        local_path = run_dir / "workspace" / fpath.lstrip("/")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(entry.content, encoding="utf-8")

    # Save tree text
    (run_dir / "tree.txt").write_text(tree_text, encoding="utf-8")

    # Save metadata
    meta = {
        "task_id": task_id,
        "instruction": started.instruction,
        "trial_id": started.trial_id,
        "timestamp": snapshot.timestamp,
        "file_count": len(snapshot.files),
        "total_bytes": sum(e.size for e in snapshot.files.values()),
        "file_hashes": {p: e.sha256 for p, e in sorted(snapshot.files.items())},
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"  [{task_id}] Saved to {run_dir}")
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump BitGN task workspace snapshots")
    parser.add_argument("--task-id", nargs="+", required=True, help="Task ID(s) to dump")
    parser.add_argument("--output", default="artifacts/ws_snapshots", help="Output directory")
    parser.add_argument("--benchmark", default=None, help="Benchmark ID")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs per task (for determinism testing)")
    parser.add_argument("--env-file", default=".worktrees/plan-b/.env")
    args = parser.parse_args()

    _load_env(args.env_file)

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("ERROR: BITGN_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    base_url = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    benchmark = args.benchmark or os.environ.get("BITGN_BENCHMARK", "bitgn/pac1-prod")
    output_dir = Path(args.output)

    print(f"Benchmark: {benchmark}")
    print(f"Base URL:  {base_url}")
    print(f"Output:    {output_dir}")
    print(f"Tasks:     {args.task_id}")
    print(f"Runs:      {args.runs}")
    print()

    harness = BitgnHarness.from_env(
        benchmark=benchmark,
        bitgn_base_url=base_url,
        bitgn_api_key=api_key,
    )

    for task_id in args.task_id:
        for run in range(args.runs):
            print(f"--- {task_id} run {run} ---")
            try:
                dump_workspace(harness, task_id, output_dir, run_index=run)
            except Exception as exc:
                print(f"  [{task_id}] ERROR: {exc}", file=sys.stderr)
            print()


if __name__ == "__main__":
    main()
