#!/usr/bin/env python3
"""Run the agent against a local workspace snapshot — no PROD VM needed.

Use this to iterate on prompts/tools/validators without burning real
ECOM trials. Works in two modes:

  1. Live LLM (default): wires the real Backend (cliproxyapi or any
     OpenAI-compatible endpoint) so the agent actually plans and acts.
     Useful for measuring how prompt/tool changes affect step count
     and outcome on known-shape tasks.

  2. Prepass-only (`--prepass-only`): runs just the bootstrap fan-out
     (tree+read(/AGENTS.MD)+context) and prints the result. No LLM
     call, no tokens burned. Useful for sanity-checking a snapshot's
     shape and the LocalEcomClient response wiring.

Examples:

    python scripts/local_runner.py \\
        --workspace tests/fixtures/local_ecom \\
        --instruction "How many paid orders are in the catalogue?"

    python scripts/local_runner.py \\
        --workspace tests/fixtures/local_ecom \\
        --instruction-file tests/fixtures/local_ecom/instructions/t01.txt \\
        --context-date 2026-05-08T12:00:00Z

    python scripts/local_runner.py \\
        --workspace tests/fixtures/local_ecom \\
        --prepass-only

Environment variables (live-LLM mode only):
    CLIPROXY_BASE_URL   — OpenAI-compat endpoint (e.g. http://127.0.0.1:8317)
    CLIPROXY_API_KEY    — proxy key
    AGENT_MODEL         — model id (default: gpt-5.3-codex)
    MAX_STEPS           — step cap (default: 40)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from bitgn_contest_agent.adapter.ecom import EcomAdapter
from bitgn_contest_agent.adapter.ecom_tracing import TracingEcomClient
from bitgn_contest_agent.agent import AgentLoop
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.local.ecom_client import LocalEcomClient
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.trace_schema import TraceMeta, TRACE_SCHEMA_VERSION
from bitgn_contest_agent.trace_writer import TraceWriter


def _read_instruction(args: argparse.Namespace) -> str:
    if args.instruction and args.instruction_file:
        raise SystemExit(
            "error: pass exactly one of --instruction / --instruction-file"
        )
    if args.instruction_file:
        return Path(args.instruction_file).read_text(encoding="utf-8").strip()
    if args.instruction:
        return args.instruction
    raise SystemExit(
        "error: pass --instruction <text> or --instruction-file <path> "
        "(or use --prepass-only to skip)"
    )


def _summarize_dataclass(obj: Any) -> Any:
    """Recursively coerce LocalEcomClient response dataclasses to plain
    dicts so we can json-dump them. Lists and nested dataclasses are
    flattened the same way."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _summarize_dataclass(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_summarize_dataclass(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _summarize_dataclass(v) for k, v in obj.items()}
    return obj


def _run_prepass_only(
    *, workspace: Path, context_date: str | None,
) -> int:
    """Run only the prepass fan-out and pretty-print the bootstrap
    bundle the agent loop would inject. No LLM, no tokens."""
    runtime = LocalEcomClient(workspace, context_date=context_date)
    traced = TracingEcomClient(runtime, writer=None)
    adapter = EcomAdapter(runtime=traced, max_tool_result_bytes=64 * 1024)

    session = Session()
    writer = _NullWriter()
    result = adapter.run_prepass(session=session, trace_writer=writer)

    print(f"Workspace: {workspace}")
    print(f"Context date: {context_date or '(now)'}")
    print(f"Prepass produced {len(result.bootstrap_content)} bundle(s):")
    for i, bundle in enumerate(result.bootstrap_content, 1):
        head = bundle.splitlines()[0] if bundle else ""
        print(f"  [{i}] {head[:120]}{'…' if len(head) > 120 else ''}")
        print(f"      ({len(bundle)} bytes)")
    print(f"\nidentity_loaded={session.identity_loaded} "
          f"rulebook_loaded={session.rulebook_loaded}")
    print(f"ops issued: {len(runtime.ops_log)}")
    for op in runtime.ops_log:
        print(f"  - {op.get('op')} {op}")
    return 0


class _NullWriter:
    """TraceWriter stand-in for the prepass-only path. The real
    TraceWriter expects a file path and a TraceMeta; the adapter only
    calls .append_prepass on it during run_prepass."""

    def append_prepass(self, **_kw: Any) -> None:
        pass

    def append_ecom_op(self, **_kw: Any) -> None:
        pass


def _build_backend() -> OpenAIChatBackend:
    """Construct an OpenAI-compat backend from environment variables.

    Mirrors cli.py:_make_backend but in a much simpler form — the local
    runner doesn't need the multi-backend selection logic. Uses the
    same `from_config` factory as PROD so the wire path is identical.
    """
    base_url = os.environ.get("CLIPROXY_BASE_URL")
    api_key = os.environ.get("CLIPROXY_API_KEY") or os.environ.get(
        "OPENAI_API_KEY", ""
    )
    if not base_url:
        raise SystemExit(
            "error: CLIPROXY_BASE_URL not set. Either start cliproxyapi "
            "and export the env, or pass --prepass-only."
        )
    if not api_key:
        raise SystemExit(
            "error: CLIPROXY_API_KEY (or OPENAI_API_KEY) not set."
        )
    model = os.environ.get("AGENT_MODEL", "gpt-5.3-codex")
    reasoning = os.environ.get("AGENT_REASONING_EFFORT", "medium")
    return OpenAIChatBackend.from_config(
        base_url=base_url,
        api_key=api_key,
        model=model,
        reasoning_effort=reasoning,
    )


def _run_full_loop(
    *,
    workspace: Path,
    instruction: str,
    task_id: str,
    context_date: str | None,
    log_path: Path,
    max_steps: int,
) -> int:
    runtime = LocalEcomClient(workspace, context_date=context_date)
    traced = TracingEcomClient(runtime, writer=None)
    adapter = EcomAdapter(runtime=traced, max_tool_result_bytes=64 * 1024)
    backend = _build_backend()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    writer = TraceWriter(path=log_path)
    traced.set_writer(writer)
    writer.write_meta(TraceMeta(
        agent_version="local",
        agent_commit="local",
        model=getattr(backend, "_model", "?"),
        backend="openai_compat",
        reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "medium"),
        benchmark="local-harness",
        task_id=task_id,
        task_index=0,
        started_at=context_date or "",
        trace_schema_version=TRACE_SCHEMA_VERSION,
        harness_url=str(workspace),
    ))
    writer.append_task(task_id=task_id, task_text=instruction)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=max_steps,
        llm_http_timeout_sec=float(os.environ.get("LLM_HTTP_TIMEOUT_SEC", "60")),
        cancel_event=threading.Event(),
    )
    result = loop.run(task_id=task_id, task_text=instruction)
    writer.close()

    # The terminal `report_completion` is recorded as a `kind=step`
    # record (the agent emitted it as a NextStep tool call); the
    # outcome string is duplicated on the `kind=outcome` record at the
    # end of the trace via AgentLoopResult.reported. Pull both so the
    # user sees outcome + message + refs in one place.
    report = _last_report_from_trace(log_path)

    print()
    print("=" * 60)
    print(f"Task:        {task_id}")
    print(f"Terminated:  {result.terminated_by}")
    print(f"Outcome:     {result.reported or '(no report)'}")
    print(f"Steps:       {result.total_steps}")
    print(f"LLM calls:   {result.total_llm_calls}")
    print(f"Prompt tok:  {result.total_prompt_tokens}")
    print(f"Refs:        {report.get('grounding_refs', [])}")
    msg = report.get("message")
    if msg:
        msg_preview = msg.replace("\n", " ")[:400]
        print(f"Message:     {msg_preview}")
    if result.error_msg:
        print(f"Error:       {result.error_kind}: {result.error_msg}")
    print(f"Trace:       {log_path}")
    print("=" * 60)
    print(f"\nLocalEcomClient ops issued: {len(runtime.ops_log)}")
    return 0 if result.reported == "OUTCOME_OK" else 1


def _last_report_from_trace(log_path: Path) -> dict[str, Any]:
    """Return the `function` payload of the last `report_completion`
    step in the trace, so the runner can show grounding_refs + the
    full message body. Returns {} when no such record exists."""
    if not log_path.exists():
        return {}
    try:
        for line in reversed(log_path.read_text().splitlines()):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "step":
                continue
            fn = (rec.get("next_step") or {}).get("function") or {}
            if fn.get("tool") == "report_completion":
                return fn
    except OSError:
        pass
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="local_runner",
        description="Run the BitGN ECOM agent against a local workspace.",
    )
    parser.add_argument(
        "--workspace", required=True, type=Path,
        help="Filesystem root to serve as the ECOM workspace",
    )
    parser.add_argument("--instruction", default=None)
    parser.add_argument("--instruction-file", default=None, type=Path)
    parser.add_argument("--task-id", default="local")
    parser.add_argument(
        "--context-date", default=None,
        help='ISO8601, e.g. "2026-05-08T12:00:00Z" (default: now)',
    )
    parser.add_argument(
        "--prepass-only", action="store_true",
        help="Run just the bootstrap fan-out, no LLM call",
    )
    parser.add_argument(
        "--log-dir", default="logs/local", type=Path,
    )
    parser.add_argument(
        "--max-steps", type=int,
        default=int(os.environ.get("MAX_STEPS", "40")),
    )
    args = parser.parse_args(argv)

    if not args.workspace.exists():
        raise SystemExit(f"workspace not found: {args.workspace}")

    if args.prepass_only:
        return _run_prepass_only(
            workspace=args.workspace,
            context_date=args.context_date,
        )

    instruction = _read_instruction(args)
    log_path = args.log_dir / f"{args.task_id}.jsonl"
    return _run_full_loop(
        workspace=args.workspace,
        instruction=instruction,
        task_id=args.task_id,
        context_date=args.context_date,
        log_path=log_path,
        max_steps=args.max_steps,
    )


if __name__ == "__main__":
    sys.exit(main())
