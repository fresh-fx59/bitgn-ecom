#!/usr/bin/env python3
"""Run the agent on PROD against ONLY a filtered task subset.

Cheap focused validation against `bitgn/ecom1-dev`: starts a real
leaderboard run, but only provisions and executes the trials whose
revealed task_id matches the filter. All other trials are closed
immediately with NONE_CLARIFICATION (no LLM cost, no VM walk).

Usage:
    scripts/run_filtered_bench.py --tasks t43,t44 --runs 1

Writes:
    logs/<ts>/<task>__run0.jsonl     — agent trace per executed task
    artifacts/bench/filtered_<ts>.json — bench summary for graded tasks
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Make src importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bitgn_contest_agent.adapter.ecom import EcomAdapter
from bitgn_contest_agent.adapter.ecom_tracing import TracingEcomClient
from bitgn_contest_agent.agent import AgentLoop
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.config import AgentConfig
from bitgn_contest_agent.harness import BitgnHarness
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.trace_schema import (
    TraceMeta, TraceOutcome, TRACE_SCHEMA_VERSION,
)
from bitgn_contest_agent.trace_writer import TraceWriter
from bitgn_contest_agent.router import load_router
from bitgn_contest_agent.reactive_router import load_reactive_router

from bitgn.vm.ecom import ecom_pb2


def _git_commit_short() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"]
        ).decode().strip()
    except Exception:
        return "unknown"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", required=True,
                   help="comma-separated task IDs to actually run, e.g. t43,t44")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--log-dir", default=None)
    p.add_argument("--output", default=None,
                   help="bench summary path (default artifacts/bench/filtered_<ts>.json)")
    args = p.parse_args()

    target_tasks = set(t.strip() for t in args.tasks.split(",") if t.strip())
    if not target_tasks:
        print("no targets", file=sys.stderr)
        return 1

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(args.log_dir or f"logs/filtered_{ts}")
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(
        args.output or f"artifacts/bench/filtered_{ts}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from bitgn_contest_agent.config import load_from_env
    cfg = load_from_env()
    skills_dir = Path(__file__).parent.parent / "src" / "bitgn_contest_agent" / "skills"
    router = load_router(skills_dir=skills_dir)
    reactive_router = load_reactive_router(skills_dir / "reactive")

    api_key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")
    harness = BitgnHarness.from_env(
        benchmark=bench, bitgn_base_url=base, bitgn_api_key=api_key,
    )

    backend = OpenAIChatBackend.from_config(
        base_url=os.environ["CLIPROXY_BASE_URL"],
        api_key=os.environ["CLIPROXY_API_KEY"],
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )

    sha = _git_commit_short()
    run_name = (
        f"@ai_engineer_helper DEV-ECOM1 {sha} "
        f"filtered-{'-'.join(sorted(target_tasks))}-{ts}"
    )
    print(f"[filtered] benchmark={bench}")
    print(f"[filtered] targets={sorted(target_tasks)}")
    print(f"[filtered] run name: {run_name}")
    rid, trial_ids = harness.start_run(name=run_name)
    print(f"[filtered] run_id={rid}  trial_ids={len(trial_ids)}")

    rows = []
    cancel = threading.Event()
    for i, tid in enumerate(trial_ids, 1):
        try:
            started = harness.start_trial(tid)
        except Exception as exc:
            print(f"  [{i:>2}/{len(trial_ids)}] start_trial failed: {exc}")
            continue
        task_id = started.task_id
        if task_id not in target_tasks:
            # Close immediately - no LLM, no probing
            try:
                started.runtime_client.answer(ecom_pb2.AnswerRequest(
                    message="filtered out — running only target tasks",
                    outcome=ecom_pb2.Outcome.OUTCOME_NONE_CLARIFICATION,
                    refs=[],
                ))
            except Exception as exc:
                print(f"     answer failed: {exc}")
            try:
                score, detail = harness.end_task(started)
            except Exception:
                pass
            print(f"  [{i:>2}/{len(trial_ids)}] {task_id} → SKIP")
            continue

        print(f"  [{i:>2}/{len(trial_ids)}] {task_id} → RUN")
        for run_index in range(args.runs):
            trace_path = log_dir / f"{task_id}__run{run_index}.jsonl"
            writer = TraceWriter(path=trace_path)
            tracing_runtime = TracingEcomClient(started.runtime_client)
            tracing_runtime.set_writer(writer)
            adapter = EcomAdapter(
                runtime=tracing_runtime,
                max_tool_result_bytes=cfg.max_tool_result_bytes,
            )
            writer.write_meta(TraceMeta(
                agent_version="filtered",
                agent_commit=sha,
                model=cfg.model,
                backend="openai_compat",
                reasoning_effort=cfg.reasoning_effort,
                benchmark=bench,
                task_id=task_id,
                task_index=i,
                started_at=datetime.now(timezone.utc).isoformat(),
                trace_schema_version=TRACE_SCHEMA_VERSION,
                harness_url=started.harness_url,
                intent_head=started.instruction[:240],
            ))
            writer.append_task(task_id=task_id, task_text=started.instruction)
            loop = AgentLoop(
                backend=backend,
                adapter=adapter,
                writer=writer,
                max_steps=cfg.max_steps,
                llm_http_timeout_sec=float(cfg.llm_http_timeout_sec),
                cancel_event=cancel,
                router=router,
                reactive_router=reactive_router,
            )
            t0 = time.time()
            try:
                result = loop.run(
                    task_id=task_id, task_text=started.instruction,
                )
                terminated_by = result.terminated_by
            except Exception as exc:
                print(f"     run crashed: {exc}")
                terminated_by = "error"
            writer.close()
            wall = time.time() - t0
            score, detail = harness.end_task(started)
            try:
                writer.patch_outcome_score(
                    float(score),
                    score_detail=[str(s) for s in detail] if detail else None,
                )
            except Exception:
                pass
            print(f"     run{run_index}  score={score:.3f}  wall={wall:.1f}s  detail={detail}")
            rows.append({
                "task_id": task_id, "run_index": run_index,
                "score": float(score), "score_detail": [str(s) for s in detail],
                "wall_seconds": wall,
                "terminated_by": terminated_by,
            })

    try:
        harness.submit_run(rid, force=True)
    except Exception as exc:
        print(f"[filtered] submit_run failed: {exc}")

    summary = {
        "benchmark": bench,
        "run_id": rid,
        "ts": ts,
        "targets": sorted(target_tasks),
        "rows": rows,
    }
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[filtered] summary: {out_path}")
    print(f"[filtered] traces: {log_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
