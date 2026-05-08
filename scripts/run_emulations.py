#!/usr/bin/env -S .venv/bin/python3 -u
"""Run per-task PROD failure emulations against the local harness.

Each emulation lives under `artifacts/ws_snapshots/emul_*/run_0/` and
contains a `workspace/` tree plus a `metadata.json` describing the
exact PROD-failing instruction and the grader expectation pulled from
the server score_detail.

Unlike `local_bench.py --canonical` (which uses 3 hand-built proxy
tasks), each emulation here corresponds to one specific PROD failure.
The grader is pinned to the same shape the server-grader checks:

  - `expected_answer`          → string match (case-insensitive)
  - `expected_writes`          → set of paths the agent must have
                                  written, each matching `expected_write_pattern`
  - `expected_outcome`         → terminal outcome (OUTCOME_OK etc.)

Usage:
    set -a && source .worktrees/plan-b/.env && set +a
    uv run python scripts/run_emulations.py
    uv run python scripts/run_emulations.py --filter emul_t066
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from local_bench import LocalPcmAdapter
from local_pcm import LocalPcmClient

from bitgn_contest_agent.adapter.pcm_tracing import TracingPcmClient
from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.bench.run_metrics import RunMetrics
from bitgn_contest_agent.config import load_from_env
from bitgn_contest_agent.reactive_router import load_reactive_router
from bitgn_contest_agent.router import load_router
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter

_LOG = logging.getLogger(__name__)


@dataclass
class EmulationResult:
    name: str
    passed: bool
    detail: str
    answer: str
    outcome: str
    steps: int
    writes_seen: list[str]
    wall_sec: float


def _grade_answer(answer: str, expected: str) -> tuple[bool, str]:
    if not answer:
        return False, f"no answer; expected={expected!r}"
    a = answer.strip().lower()
    e = expected.strip().lower()
    if e in a:
        return True, f"answer contains {expected!r}"
    return False, f"answer={answer.strip()[:120]!r} expected={expected!r}"


def _grade_writes(
    writes: dict[str, str],
    expected_paths: list[str],
    pattern: str,
) -> tuple[bool, str]:
    """Verify each expected path was written and matches `pattern`."""
    rx = re.compile(pattern, re.MULTILINE)
    # LocalPcmClient strips leading slashes when keying writes
    expected_paths = [p.lstrip("/") for p in expected_paths]
    seen = set(writes.keys())
    missing = [p for p in expected_paths if p not in seen]
    if missing:
        return False, f"missing writes: {missing}"
    bad_pattern = []
    for p in expected_paths:
        content = writes[p]
        if not rx.search(content):
            bad_pattern.append(p)
    if bad_pattern:
        return False, (
            f"writes present but pattern {pattern!r} not matched on: "
            f"{bad_pattern}"
        )
    return True, f"all {len(expected_paths)} expected writes match"


def _run_one(
    *,
    name: str,
    workspace_src: Path,
    metadata: dict,
    cfg,
    backend: OpenAIChatBackend,
    log_dir: Path,
    router: Any,
    reactive_router: Any,
) -> EmulationResult:
    instruction = metadata["instruction"]
    expected_answer = metadata.get("expected_answer")
    expected_writes = metadata.get("expected_writes")
    expected_pattern = metadata.get("expected_write_pattern", "^---\\nrecord_type:")
    expected_outcome = metadata.get("expected_outcome")
    context_date = metadata.get("context_date")

    tmp_dir = tempfile.mkdtemp(prefix=f"emul_{name}_")
    tmp_ws = Path(tmp_dir) / "workspace"
    shutil.copytree(workspace_src, tmp_ws)

    t0 = time.monotonic()
    client = LocalPcmClient(str(tmp_ws), context_date=context_date)
    trace_path = log_dir / f"{name}.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    writer = TraceWriter(path=trace_path)
    traced = TracingPcmClient(client, writer=writer)
    adapter = LocalPcmAdapter(
        client=traced, max_tool_result_bytes=cfg.max_tool_result_bytes,
    )
    writer.write_meta(TraceMeta(
        agent_version="emul",
        agent_commit="emul",
        model=cfg.model,
        backend="openai_compat",
        reasoning_effort=cfg.reasoning_effort,
        benchmark="emul",
        task_id=name,
        task_index=0,
        started_at=datetime.now(timezone.utc).isoformat(),
        trace_schema_version=TRACE_SCHEMA_VERSION,
        harness_url=None,
        intent_head=instruction[:240],
    ))

    metrics = RunMetrics(max_inflight_llm=cfg.max_inflight_llm)
    sema = threading.Semaphore(cfg.max_inflight_llm)
    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=cfg.max_steps,
        llm_http_timeout_sec=float(cfg.llm_http_timeout_sec),
        cancel_event=threading.Event(),
        backend_backoff_ms=cfg.rate_limit_backoff_ms,
        inflight_semaphore=sema,
        metrics=metrics,
        router=router,
        reactive_router=reactive_router,
    )
    try:
        result: AgentLoopResult = loop.run(task_id=name, task_text=instruction)
    except Exception as exc:
        writer.close()
        return EmulationResult(
            name=name, passed=False,
            detail=f"crash: {type(exc).__name__}: {exc!s}",
            answer="", outcome="error", steps=0,
            writes_seen=[], wall_sec=time.monotonic() - t0,
        )
    writer.close()

    answer = ""
    outcome = result.terminated_by
    if adapter.last_answer:
        answer = adapter.last_answer.get("message", "")
        outcome = adapter.last_answer.get("outcome", outcome)

    writes_dict = dict(client.writes)
    writes_seen = sorted(writes_dict.keys())

    # Apply graders in priority order
    if expected_writes:
        passed, detail = _grade_writes(writes_dict, expected_writes, expected_pattern)
    elif expected_answer:
        passed, detail = _grade_answer(answer, expected_answer)
    elif expected_outcome:
        passed = outcome == expected_outcome
        detail = f"outcome={outcome} expected={expected_outcome}"
    else:
        passed, detail = False, "no grader configured in metadata.json"

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return EmulationResult(
        name=name, passed=passed, detail=detail,
        answer=answer.strip()[:200], outcome=outcome,
        steps=result.total_steps, writes_seen=writes_seen,
        wall_sec=round(time.monotonic() - t0, 1),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="run_emulations")
    parser.add_argument("--filter", default="",
                        help="substring filter on emulation name")
    parser.add_argument("--snap-root", type=Path,
                        default=_REPO / "artifacts" / "ws_snapshots")
    parser.add_argument("--log-dir", type=Path,
                        default=_REPO / "logs" / "emulations")
    args = parser.parse_args()

    import os
    if not os.environ.get("BITGN_API_KEY"):
        os.environ["BITGN_API_KEY"] = "emul-dummy"
    cfg = load_from_env()
    backend = OpenAIChatBackend.from_config(
        base_url=cfg.cliproxy_base_url,
        api_key=cfg.cliproxy_api_key,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )
    skills_dir = _REPO / "src" / "bitgn_contest_agent" / "skills"
    router = load_router(skills_dir=skills_dir)
    reactive_router = load_reactive_router(skills_dir / "reactive")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_dir = args.log_dir / ts
    log_dir.mkdir(parents=True, exist_ok=True)

    emulations = []
    for snap_dir in sorted(args.snap_root.iterdir()):
        if not snap_dir.is_dir():
            continue
        if not snap_dir.name.startswith("emul_"):
            continue
        if args.filter and args.filter not in snap_dir.name:
            continue
        run0 = snap_dir / "run_0"
        meta_file = run0 / "metadata.json"
        ws_dir = run0 / "workspace"
        if not (meta_file.exists() and ws_dir.exists()):
            continue
        emulations.append((snap_dir.name, ws_dir, json.loads(meta_file.read_text())))

    if not emulations:
        print("No emulations found under", args.snap_root)
        return 1

    print(f"Running {len(emulations)} emulation(s) → {log_dir}")
    print(f"  model={cfg.model} reasoning={cfg.reasoning_effort}")
    print(f"  OPT_A_CASE_INSENSITIVE={os.getenv('BITGN_OPT_A_CASE_INSENSITIVE','')!r} "
          f"OPT_A_FIND_CI={os.getenv('BITGN_OPT_A_FIND_CI','')!r}")
    print()

    results: list[EmulationResult] = []
    for name, ws, meta in emulations:
        print(f"→ {name}: {meta['instruction'][:80]!r}")
        r = _run_one(
            name=name, workspace_src=ws, metadata=meta,
            cfg=cfg, backend=backend, log_dir=log_dir,
            router=router, reactive_router=reactive_router,
        )
        results.append(r)
        tag = "PASS" if r.passed else "FAIL"
        print(f"  {tag} | {r.detail}")
        print(f"  outcome={r.outcome} steps={r.steps} answer={r.answer[:120]!r} "
              f"wall={r.wall_sec}s")
        if not r.passed and r.writes_seen:
            print(f"  writes_seen ({len(r.writes_seen)}):")
            for p in r.writes_seen[:10]:
                print(f"    {p}")
        print()

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print("=" * 60)
    print(f"Results: {passed}/{total} passed")
    for r in results:
        tag = "PASS" if r.passed else "FAIL"
        print(f"  {r.name}: {tag} ({r.detail})")

    summary_path = log_dir / "results.json"
    summary_path.write_text(json.dumps([
        {
            "name": r.name, "passed": r.passed, "detail": r.detail,
            "answer": r.answer, "outcome": r.outcome, "steps": r.steps,
            "writes_seen": r.writes_seen, "wall_sec": r.wall_sec,
        } for r in results
    ], indent=2))
    print(f"Results → {summary_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
