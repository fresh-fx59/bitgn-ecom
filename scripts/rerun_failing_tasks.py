"""Re-run specific failing tasks N times via playground flow to separate flaky from persistent."""
from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from bitgn_contest_agent.cli import (
    _make_backend,
    _make_harness,
    _resolve_config,
    _run_single_task,
    _get_router,
    _get_reactive_router,
)
from bitgn_contest_agent.orchestrator import TaskSpec


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run failing tasks via playground")
    parser.add_argument("--tasks", required=True, help="Comma-separated task IDs (e.g. t005,t030)")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per task")
    parser.add_argument("--max-parallel", type=int, default=10, help="Max parallel tasks")
    parser.add_argument("--max-inflight-llm", type=int, default=None,
                        help="Max concurrent LLM calls across all parallel tasks")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    task_ids = [t.strip() for t in args.tasks.split(",")]
    runs = args.runs
    max_parallel = args.max_parallel

    # Build config from env vars (same as CLI)
    ns = argparse.Namespace(
        benchmark="bitgn/pac1-prod",
        max_parallel=None,
        max_inflight_llm=None,
        output=None,
        runs=runs,
        smoke=False,
        parallel_iterations=None,
        no_parallel_iterations=None,
        log_dir=None,
    )
    cfg = _resolve_config(ns)
    if args.max_inflight_llm is not None:
        import dataclasses
        cfg = dataclasses.replace(cfg, max_inflight_llm=args.max_inflight_llm)
    harness = _make_harness(cfg)
    backend = _make_backend(cfg)
    router = _get_router()
    reactive_router = _get_reactive_router()
    inflight_semaphore = threading.Semaphore(cfg.max_inflight_llm)

    results: dict[str, list[dict]] = {tid: [] for tid in task_ids}
    results_lock = threading.Lock()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"Re-running {len(task_ids)} tasks x {runs} runs via playground flow")
    print(f"Tasks: {', '.join(task_ids)}")
    print(f"Parallelism: {max_parallel} tasks concurrent")
    print(flush=True)

    def run_one(tid: str, run_idx: int) -> tuple[str, int, dict]:
        task = TaskSpec(task_id=tid, task_index=0, task_text="")
        result = _run_single_task(
            cfg=cfg,
            harness=harness,
            backend=backend,
            task=task,
            run_id=run_id,
            run_index=run_idx,
            cancel_event=threading.Event(),
            inflight_semaphore=inflight_semaphore,
            router=router,
            reactive_router=reactive_router,
        )
        passed = result.score >= 1.0
        entry = {
            "run": run_idx,
            "score": result.score,
            "terminated_by": result.terminated_by,
            "passed": passed,
        }
        status = "PASS" if passed else "FAIL"
        print(f"  {tid} run {run_idx + 1}: {status} (score={result.score})", flush=True)
        return tid, run_idx, entry

    # Build all (task, run) pairs and run in parallel
    work = [(tid, run_idx) for run_idx in range(runs) for tid in task_ids]

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(run_one, tid, ri): (tid, ri) for tid, ri in work}
        for fut in as_completed(futures):
            tid, run_idx, entry = fut.result()
            with results_lock:
                results[tid].append(entry)

    # Summary
    print(flush=True)
    print("=== SUMMARY ===", flush=True)
    summary = {}
    for tid in task_ids:
        passes = sum(1 for r in results[tid] if r["passed"])
        total = len(results[tid])
        label = "PERSISTENT" if passes == 0 else ("FLAKY" if passes < total else "FIXED")
        print(f"  {tid}: {passes}/{total} passed — {label}", flush=True)
        summary[tid] = {"passes": passes, "total": total, "label": label, "runs": results[tid]}

    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nResults written to {args.output}", flush=True)

    persistent = [tid for tid, s in summary.items() if s["label"] == "PERSISTENT"]
    flaky = [tid for tid, s in summary.items() if s["label"] == "FLAKY"]
    fixed = [tid for tid, s in summary.items() if s["label"] == "FIXED"]

    print(f"\nPersistent failures: {len(persistent)} — {', '.join(persistent) or 'none'}", flush=True)
    print(f"Flaky: {len(flaky)} — {', '.join(flaky) or 'none'}", flush=True)
    print(f"Fixed: {len(fixed)} — {', '.join(fixed) or 'none'}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
