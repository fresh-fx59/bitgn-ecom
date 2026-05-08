"""Capture workspace snapshot from a running PROD task.

Connects to the PROD PCM server, reads the full file tree,
and saves it as a local workspace snapshot for offline replay.

Usage:
    # Capture from a PROD trace directory (reads harness URL from trace)
    python scripts/capture_workspace.py \
        --trace logs/29bbca5_norm_smoke/t042__run0.jsonl \
        --instruction "How much did X charge..." \
        --expected "42" \
        --output artifacts/ws_snapshots/t042

    # Capture from explicit harness URL
    python scripts/capture_workspace.py \
        --harness-url https://vm-xxx.eu.bitgn.com \
        --task-id t042 \
        --instruction "How much did X charge..." \
        --output artifacts/ws_snapshots/t042

    # Batch capture all failed tasks from a PROD results file
    python scripts/capture_workspace.py \
        --from-results artifacts/bench/29bbca5_norm_smoke_p3i6_prod_runs1.json \
        --log-dir logs/29bbca5_norm_smoke \
        --output-dir artifacts/ws_snapshots
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def extract_task_info_from_trace(trace_path: str) -> dict:
    """Extract task_text and harness metadata from a trace JSONL file."""
    info = {}
    with open(trace_path) as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("kind") == "meta":
                info["harness_url"] = entry.get("harness_url", "")
                info["task_id"] = entry.get("task_id", "")
                info["instruction"] = entry.get("intent_head", "")
            if entry.get("kind") == "task":
                info["instruction"] = entry.get("task_text", "")
    return info


def capture_from_pcm(harness_url: str, output_dir: Path) -> int:
    """Capture workspace by calling tree + read on all files via PCM."""
    # Import PCM client
    sys.path.insert(0, str(_REPO / "src"))
    from bitgn.vm import pcm_pb2
    from bitgn.vm.pcm_connect import PcmRuntimeClientSync

    runtime = PcmRuntimeClientSync(harness_url)

    # Get file tree
    tree_resp = runtime.tree(pcm_pb2.TreeRequest(root="/"))

    ws_dir = output_dir / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    def _walk_tree(entry, prefix=""):
        path = f"{prefix}/{entry.name}" if prefix else f"/{entry.name}"
        if entry.is_dir:
            for child in entry.children:
                yield from _walk_tree(child, path)
        else:
            yield path

    files_captured = 0
    for file_path in _walk_tree(tree_resp.root):
        try:
            read_resp = runtime.read(pcm_pb2.ReadRequest(path=file_path))
            local_path = ws_dir / file_path.lstrip("/")
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(read_resp.content, encoding="utf-8")
            files_captured += 1
        except Exception as exc:
            print(f"  WARN: failed to read {file_path}: {exc}")

    print(f"  Captured {files_captured} files → {ws_dir}")
    return files_captured


def save_metadata(output_dir: Path, instruction: str, expected: str,
                  context_date: str = "", source: str = "prod_capture"):
    """Save metadata.json alongside the workspace snapshot."""
    meta = {
        "instruction": instruction,
        "expected_answer": expected,
        "context_date": context_date or "2026-04-13T10:00:00Z",
        "source": source,
    }
    meta_path = output_dir / "metadata.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"  Metadata → {meta_path}")


def main():
    parser = argparse.ArgumentParser(description="Capture PROD workspace snapshots")
    parser.add_argument("--trace", help="Path to trace JSONL file")
    parser.add_argument("--harness-url", help="PROD harness URL")
    parser.add_argument("--task-id", help="Task ID")
    parser.add_argument("--instruction", help="Task instruction text")
    parser.add_argument("--expected", default="", help="Expected answer")
    parser.add_argument("--output", help="Output directory for single capture")
    parser.add_argument("--from-results", help="PROD results JSON — capture all failed tasks")
    parser.add_argument("--log-dir", help="Log directory with traces (for --from-results)")
    parser.add_argument("--output-dir", default="artifacts/ws_snapshots",
                        help="Output root for batch captures")
    args = parser.parse_args()

    if args.from_results:
        # Batch mode: capture workspaces for all failed tasks
        with open(args.from_results) as f:
            data = json.load(f)

        tasks = data.get("tasks", data) if isinstance(data, dict) else data
        log_dir = Path(args.log_dir) if args.log_dir else None
        out_root = Path(args.output_dir)

        failed = []
        if isinstance(tasks, dict):
            for tid, tdata in tasks.items():
                if isinstance(tdata, dict):
                    passes = tdata.get("passes", 0)
                    runs = tdata.get("runs", 1)
                    if passes < runs:
                        failed.append((tid, tdata))
        else:
            for t in tasks:
                if not t.get("passed", True):
                    failed.append((t.get("task_id", ""), t))

        print(f"Found {len(failed)} failed tasks")
        for tid, tdata in failed:
            snap_dir = out_root / tid / "run_0"
            if (snap_dir / "workspace").exists():
                print(f"  {tid}: snapshot already exists, skipping")
                continue

            # Find trace file
            trace_file = None
            if log_dir:
                candidates = list(log_dir.glob(f"{tid}__run0.jsonl")) + \
                             list(log_dir.glob(f"{tid}_*.jsonl"))
                if candidates:
                    trace_file = str(candidates[0])

            if trace_file:
                info = extract_task_info_from_trace(trace_file)
                harness_url = info.get("harness_url", "")
                instruction = info.get("instruction", "")
                if harness_url:
                    print(f"\n[{tid}] Capturing from {harness_url}")
                    try:
                        capture_from_pcm(harness_url, snap_dir)
                        save_metadata(snap_dir, instruction, "",
                                      source="prod_capture_batch")
                    except Exception as exc:
                        print(f"  ERROR: {exc}")
                else:
                    print(f"  {tid}: no harness_url in trace, skipping")
            else:
                print(f"  {tid}: no trace file found, skipping")

    elif args.trace or args.harness_url:
        # Single capture mode
        if args.trace:
            info = extract_task_info_from_trace(args.trace)
            harness_url = args.harness_url or info.get("harness_url", "")
            instruction = args.instruction or info.get("instruction", "")
            task_id = args.task_id or info.get("task_id", "unknown")
        else:
            harness_url = args.harness_url
            instruction = args.instruction or ""
            task_id = args.task_id or "unknown"

        output = Path(args.output or f"artifacts/ws_snapshots/{task_id}/run_0")
        print(f"Capturing {task_id} from {harness_url}")
        capture_from_pcm(harness_url, output)
        save_metadata(output, instruction, args.expected,
                      source="prod_capture_single")
    else:
        parser.error("Specify --trace, --harness-url, or --from-results")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
