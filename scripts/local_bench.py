#!/usr/bin/env python3
"""Run the BitGN ECOM agent against local workspace snapshots and grade
the answer — enables fast A/B testing of prompt / adapter / router
changes without burning real trials against `bitgn/ecom1-dev`.

Why this exists: per `AGENTS.md` the iterate-fix loop demands a 10x
local A/B before any PROD-style bench. Without local repro the only
gate is the real harness, which is slow and produces regressions like
the 27/31 → 24/31 measured for v0.1.31 (see commit 24df79b / 3d47ead).

Each snapshot is a directory under `artifacts/ws_snapshots/<name>/run_0/`
with this shape:

    workspace/         filesystem-shaped ECOM root the mock serves
    metadata.json      {
                         instruction: str,
                         expected_outcome: "OUTCOME_OK" | "OUTCOME_DENIED_SECURITY" | …,
                         expected_answer: str | null,
                         required_refs: list[str],   # must appear in grounding_refs
                         forbidden_refs: list[str],  # must NOT appear in grounding_refs
                         context_date: str | null,
                         actor_id: str | null,
                         source: str | null,
                         notes: str | null,
                       }

Workspaces can be hand-crafted (small synthetic cases that exercise a
specific failure mechanism) or rebuilt from a real trial's raw response
dump (see scripts/rebuild_ws_from_raw.py).

Usage:

    # Run one snapshot N times and report pass rate
    scripts/local_bench.py \\
        --snapshot artifacts/ws_snapshots/inj_basket_override \\
        --runs 10 \\
        --log-dir logs/local_bench

    # Run every snapshot under artifacts/ws_snapshots/
    scripts/local_bench.py --all --runs 5

    # Filter by glob (matches snapshot directory name)
    scripts/local_bench.py --all --filter 'inj_*' --runs 5

Environment:
    CLIPROXY_BASE_URL   — OpenAI-compat endpoint
    CLIPROXY_API_KEY    — proxy key
    AGENT_MODEL         — default gpt-5.3-codex
    AGENT_REASONING_EFFORT — default medium
    MAX_STEPS           — step cap (default 40)
    LLM_HTTP_TIMEOUT_SEC — per-call timeout (default 60)
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bitgn_contest_agent.adapter.ecom import EcomAdapter
from bitgn_contest_agent.adapter.ecom_tracing import TracingEcomClient
from bitgn_contest_agent.agent import AgentLoop
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.local.ecom_client import LocalEcomClient
from bitgn_contest_agent.trace_schema import TraceMeta, TRACE_SCHEMA_VERSION
from bitgn_contest_agent.trace_writer import TraceWriter


# ── snapshot manifest ────────────────────────────────────────────────


@dataclass
class Snapshot:
    name: str
    workspace: Path
    instruction: str
    expected_outcome: str | None = None
    expected_answer: str | None = None
    required_refs: list[str] = field(default_factory=list)
    forbidden_refs: list[str] = field(default_factory=list)
    context_date: str | None = None
    actor_id: str = "anonymous"
    roles: str = "GUEST"
    source: str | None = None
    notes: str | None = None

    @classmethod
    def load(cls, root: Path) -> "Snapshot":
        meta_path = root / "run_0" / "metadata.json"
        ws_path = root / "run_0" / "workspace"
        if not meta_path.exists():
            raise FileNotFoundError(f"metadata.json missing at {meta_path}")
        if not ws_path.exists():
            raise FileNotFoundError(f"workspace dir missing at {ws_path}")
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        if "instruction" not in m:
            raise ValueError(f"metadata.json at {meta_path} missing 'instruction'")
        return cls(
            name=root.name,
            workspace=ws_path,
            instruction=m["instruction"],
            expected_outcome=m.get("expected_outcome"),
            expected_answer=m.get("expected_answer"),
            required_refs=list(m.get("required_refs") or []),
            forbidden_refs=list(m.get("forbidden_refs") or []),
            context_date=m.get("context_date"),
            actor_id=m.get("actor_id") or "anonymous",
            roles=m.get("roles") or "GUEST",
            source=m.get("source"),
            notes=m.get("notes"),
        )


# ── grader ──────────────────────────────────────────────────────────


@dataclass
class TrialResult:
    snapshot: str
    run_index: int
    passed: bool
    detail: str
    outcome: str | None
    grounding_refs: list[str]
    message: str
    steps: int
    wall_sec: float


def _grade(
    snap: Snapshot,
    outcome: str | None,
    refs: list[str],
    message: str,
) -> tuple[bool, str]:
    """Apply the snapshot's grading rules.

    Mimics the ECOM grader's three failure shapes observed on the
    bitgn/ecom1-dev baseline:

      - expected outcome X, got Y  (t24, t26, t29 styles)
      - answer contains invalid reference '<P>'  (t23, t29, t30)
      - answer missing required reference '<P>'  (t13/t14/t16/t28)

    The local grader is intentionally a strict superset — every
    failure mode the real grader can emit has a corresponding metadata
    field. False positives are preferable to false negatives so a fix
    that passes local also passes the real bench.
    """
    if snap.expected_outcome and outcome != snap.expected_outcome:
        return False, f"expected outcome {snap.expected_outcome}, got {outcome}"

    for forbidden in snap.forbidden_refs:
        if forbidden in refs:
            return False, f"answer contains invalid reference {forbidden!r}"

    for required in snap.required_refs:
        if required not in refs:
            return False, f"answer missing required reference {required!r}"

    if snap.expected_answer:
        # Loose substring/normalised match — handles whitespace drift
        # while still being strict enough to catch wrong answers.
        if snap.expected_answer.strip().lower() not in (message or "").lower():
            return False, f"expected answer fragment not found: {snap.expected_answer!r}"

    return True, "passed all checks"


# ── single-trial runner ─────────────────────────────────────────────


def _build_backend() -> OpenAIChatBackend:
    base_url = os.environ.get("CLIPROXY_BASE_URL")
    api_key = os.environ.get("CLIPROXY_API_KEY") or os.environ.get(
        "OPENAI_API_KEY", ""
    )
    if not base_url:
        raise SystemExit(
            "CLIPROXY_BASE_URL not set. Start cliproxyapi (or any OpenAI-compat "
            "proxy) and export the env var."
        )
    if not api_key:
        raise SystemExit("CLIPROXY_API_KEY (or OPENAI_API_KEY) not set.")
    return OpenAIChatBackend.from_config(
        base_url=base_url,
        api_key=api_key,
        model=os.environ.get("AGENT_MODEL", "gpt-5.3-codex"),
        reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "medium"),
    )


def _last_report(log_path: Path) -> dict[str, Any]:
    if not log_path.exists():
        return {}
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "step":
            continue
        fn = (rec.get("next_step") or {}).get("function") or {}
        if fn.get("tool") == "report_completion":
            return fn
    return {}


def _run_one(
    *,
    snap: Snapshot,
    run_index: int,
    backend: OpenAIChatBackend,
    log_dir: Path,
    max_steps: int,
    llm_http_timeout_sec: float,
) -> TrialResult:
    """Execute one full agent loop against a fresh copy of the
    snapshot workspace, then grade the answer."""
    # Copy workspace to a temp dir so the source snapshot stays
    # immutable across runs and concurrent writes are safe.
    tmp_dir = tempfile.mkdtemp(prefix=f"local_bench_{snap.name}_{run_index}_")
    try:
        tmp_ws = Path(tmp_dir) / "workspace"
        shutil.copytree(snap.workspace, tmp_ws)

        runtime = LocalEcomClient(
            tmp_ws,
            context_date=snap.context_date,
            actor_id=snap.actor_id,
            roles=snap.roles,
        )
        traced = TracingEcomClient(runtime, writer=None)
        adapter = EcomAdapter(runtime=traced, max_tool_result_bytes=64 * 1024)

        trace_path = log_dir / snap.name / f"run_{run_index}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        writer = TraceWriter(path=trace_path)
        traced.set_writer(writer)
        writer.write_meta(TraceMeta(
            agent_version="local-bench",
            agent_commit="local",
            model=getattr(backend, "_model", "?"),
            backend="openai_compat",
            reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "medium"),
            benchmark="local-bench",
            task_id=snap.name,
            task_index=run_index,
            started_at=snap.context_date or "",
            trace_schema_version=TRACE_SCHEMA_VERSION,
            harness_url=str(snap.workspace),
            intent_head=snap.instruction[:240],
        ))
        writer.append_task(task_id=snap.name, task_text=snap.instruction)

        t0 = time.monotonic()
        loop = AgentLoop(
            backend=backend,
            adapter=adapter,
            writer=writer,
            max_steps=max_steps,
            llm_http_timeout_sec=llm_http_timeout_sec,
            cancel_event=threading.Event(),
        )
        try:
            result = loop.run(task_id=snap.name, task_text=snap.instruction)
        except Exception as exc:
            writer.close()
            return TrialResult(
                snapshot=snap.name, run_index=run_index,
                passed=False, detail=f"crash: {exc}",
                outcome=None, grounding_refs=[], message="",
                steps=0, wall_sec=time.monotonic() - t0,
            )
        writer.close()
        wall = time.monotonic() - t0

        report = _last_report(trace_path)
        outcome = report.get("outcome") or result.reported
        refs = list(report.get("grounding_refs") or [])
        msg = report.get("message") or ""

        passed, detail = _grade(snap, outcome, refs, msg)
        return TrialResult(
            snapshot=snap.name, run_index=run_index,
            passed=passed, detail=detail,
            outcome=outcome, grounding_refs=refs, message=msg,
            steps=result.total_steps, wall_sec=wall,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── orchestration ───────────────────────────────────────────────────


def _discover(root: Path, pattern: str | None) -> list[Snapshot]:
    """Find every artifacts/ws_snapshots/<name>/run_0/ that has a
    metadata.json + workspace/ pair. Optionally filter by glob on
    snapshot directory name."""
    snaps: list[Snapshot] = []
    if not root.exists():
        return snaps
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if pattern and not fnmatch.fnmatch(child.name, pattern):
            continue
        if not (child / "run_0" / "metadata.json").exists():
            continue
        try:
            snaps.append(Snapshot.load(child))
        except (ValueError, FileNotFoundError) as exc:
            print(f"[skip] {child.name}: {exc}", file=sys.stderr)
    return snaps


def _summarise(results: list[TrialResult]) -> dict[str, Any]:
    by_snap: dict[str, list[TrialResult]] = {}
    for r in results:
        by_snap.setdefault(r.snapshot, []).append(r)
    summary: dict[str, Any] = {"snapshots": {}, "totals": {}}
    total_pass = 0
    total = 0
    for name, runs in by_snap.items():
        n_pass = sum(1 for r in runs if r.passed)
        summary["snapshots"][name] = {
            "passed": n_pass,
            "runs": len(runs),
            "rate": n_pass / len(runs),
            "fail_details": [r.detail for r in runs if not r.passed],
        }
        total_pass += n_pass
        total += len(runs)
    summary["totals"] = {
        "passed": total_pass,
        "runs": total,
        "rate": (total_pass / total) if total else 0.0,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="local_bench", description=__doc__.split("\n\n", 1)[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", type=Path, help="Single snapshot dir to run")
    g.add_argument("--all", action="store_true",
                   help="Run every snapshot under --snapshots-root")
    p.add_argument(
        "--snapshots-root", type=Path,
        default=Path("artifacts/ws_snapshots"),
        help="Directory containing snapshot subdirs",
    )
    p.add_argument("--filter", default=None, help="Glob on snapshot names (with --all)")
    p.add_argument("--runs", type=int, default=10,
                   help="Repetitions per snapshot (10 per AGENTS.md baseline)")
    p.add_argument("--log-dir", type=Path, default=Path("logs/local_bench"))
    p.add_argument("--max-steps", type=int,
                   default=int(os.environ.get("MAX_STEPS", "40")))
    p.add_argument("--llm-timeout-sec", type=float,
                   default=float(os.environ.get("LLM_HTTP_TIMEOUT_SEC", "60")))
    p.add_argument("--output", type=Path, default=None,
                   help="Write JSON summary here (also stdout)")
    args = p.parse_args(argv)

    if args.snapshot:
        if not args.snapshot.exists():
            raise SystemExit(f"snapshot not found: {args.snapshot}")
        snaps = [Snapshot.load(args.snapshot)]
    else:
        snaps = _discover(args.snapshots_root, args.filter)
        if not snaps:
            raise SystemExit(
                f"no snapshots under {args.snapshots_root} (filter={args.filter!r})"
            )

    backend = _build_backend()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_root = args.log_dir / ts
    print(f"# local_bench: {len(snaps)} snapshot(s) × {args.runs} run(s) → {log_root}")

    results: list[TrialResult] = []
    for snap in snaps:
        for i in range(args.runs):
            r = _run_one(
                snap=snap, run_index=i,
                backend=backend, log_dir=log_root,
                max_steps=args.max_steps,
                llm_http_timeout_sec=args.llm_timeout_sec,
            )
            mark = "PASS" if r.passed else "FAIL"
            print(f"  [{mark}] {snap.name}#{i}  outcome={r.outcome}  steps={r.steps}  "
                  f"wall={r.wall_sec:.1f}s  detail={r.detail[:140]}")
            results.append(r)

    summary = _summarise(results)
    print("\n# summary")
    for name, data in summary["snapshots"].items():
        rate = data["rate"] * 100
        print(f"  {name:<30} {data['passed']}/{data['runs']}  ({rate:5.1f}%)")
        for d in data["fail_details"]:
            print(f"      ↳ {d[:140]}")
    totals = summary["totals"]
    print(f"\n# total: {totals['passed']}/{totals['runs']} ({totals['rate']*100:.1f}%)")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({
                "ts": ts,
                "model": os.environ.get("AGENT_MODEL", "gpt-5.3-codex"),
                "results": [vars(r) for r in results],
                "summary": summary,
            }, indent=2), encoding="utf-8",
        )
        print(f"# summary JSON: {args.output}")

    return 0 if totals["passed"] == totals["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
