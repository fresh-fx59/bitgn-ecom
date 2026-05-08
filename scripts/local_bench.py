#!/usr/bin/env -S .venv/bin/python3 -u
"""Run the full agent loop against local workspace snapshots.

Wires LocalPcmClient → LocalPcmAdapter → AgentLoop so tasks run
against filesystem-backed sandboxes without touching the PROD server.
The LLM backend (cliproxyapi) is the only external dependency.

Usage:
    # Single task
    python scripts/local_bench.py \
        --workspace artifacts/ws_snapshots/t053/run_0/workspace \
        --instruction "In which projects is the home server involved? ..." \
        --expected "Black Library Evenings\nHearthline\n..."

    # Batch from JSON manifest
    python scripts/local_bench.py --manifest artifacts/test_cases/local_tasks.json

    # Quick run of the 3 canonical test tasks
    python scripts/local_bench.py --canonical
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from local_pcm import LocalPcmClient, TreeEntry

from bitgn_contest_agent.adapter.pcm import (
    ToolResult, PrepassResult,
    _maybe_rewrite_ci, _OPT_A_CASE_INSENSITIVE,
    _OPT_A_FIND_CI, _find_ci_variants,
)
from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.bench.run_metrics import RunMetrics
from bitgn_contest_agent.config import load_from_env, AgentConfig
from bitgn_contest_agent.format_validator import validate_yaml_frontmatter
from bitgn_contest_agent.preflight.schema import (
    WorkspaceSchema,
    discover_schema_from_fs,
    parse_schema_content,
)
from bitgn_contest_agent.preflight.semantic_index import build_digest_from_fs
from bitgn_contest_agent.reactive_router import load_reactive_router
from bitgn_contest_agent.router import load_router
from bitgn_contest_agent.schemas import (
    ReportTaskCompletion,
    Req_Context,
    Req_Delete,
    Req_Find,
    Req_List,
    Req_MkDir,
    Req_Move,
    Req_PreflightSchema,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
)
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter

_LOG = logging.getLogger(__name__)


# ── JSON serialisers matching protobuf MessageToJson output ──────────

def _tree_entry_to_dict(e: TreeEntry) -> dict:
    d: dict[str, Any] = {"name": e.name, "is_dir": e.is_dir}
    if e.children:
        d["children"] = [_tree_entry_to_dict(c) for c in e.children]
    return d


# ── LocalPcmAdapter ─────────────────────────────────────────────────

class LocalPcmAdapter:
    """Drop-in replacement for PcmAdapter that reads/writes to a local
    workspace via LocalPcmClient.  Implements dispatch(), submit_terminal(),
    and run_prepass() with identical ToolResult shapes so AgentLoop doesn't
    know the difference.
    """

    def __init__(self, *, client: LocalPcmClient, max_tool_result_bytes: int):
        self._client = client
        self._max_bytes = max_tool_result_bytes
        self.last_answer: dict | None = None

    # ── main dispatch ────────────────────────────────────────────────

    def dispatch(self, req: Any) -> ToolResult:
        start = time.monotonic()
        try:
            if isinstance(req, Req_Read):
                resp = self._client.read(req)
                return self._finish(
                    start,
                    json.dumps({"path": req.path, "content": resp.content}),
                    refs=(req.path,),
                )

            if isinstance(req, Req_Write):
                val = validate_yaml_frontmatter(req.content)
                if not val.ok:
                    wall_ms = int((time.monotonic() - start) * 1000)
                    return ToolResult(
                        ok=False, content="", refs=(),
                        error=f"YAML frontmatter parse error: {val.error}",
                        error_code="FORMAT_INVALID", wall_ms=wall_ms,
                    )
                self._client.write(req)
                return self._finish(start, "{}", refs=())

            if isinstance(req, Req_Delete):
                self._client.delete(req)
                return self._finish(start, "{}", refs=())

            if isinstance(req, Req_MkDir):
                self._client.mkdir(req)
                return self._finish(start, "{}", refs=())

            if isinstance(req, Req_Move):
                self._client.move(req)
                return self._finish(start, "{}", refs=())

            if isinstance(req, Req_List):
                resp = self._client.list(req)
                entries = [{"name": e.name, "is_dir": e.is_dir}
                           for e in resp.entries]
                return self._finish(start, json.dumps({"entries": entries}),
                                    refs=())

            if isinstance(req, Req_Tree):
                resp = self._client.tree(req)
                tree_dict = _tree_entry_to_dict(resp.root)
                return self._finish(start, json.dumps({"root": tree_dict}),
                                    refs=())

            if isinstance(req, Req_Find):
                name_in = req.name
                variants = _find_ci_variants(name_in) if _OPT_A_FIND_CI else [name_in]
                if _OPT_A_FIND_CI and len(variants) > 1:
                    union: list[str] = []
                    seen_paths: set[str] = set()
                    hits_before = -1
                    for idx, variant in enumerate(variants):
                        try:
                            r = self._client.find(
                                req.model_copy(update={"name": variant})
                            )
                        except Exception:
                            continue
                        items = list(getattr(r, "items", []) or [])
                        if idx == 0:
                            hits_before = len(items)
                        for p in items:
                            if p not in seen_paths:
                                seen_paths.add(p)
                                union.append(p)
                                if len(union) >= req.limit:
                                    break
                        if len(union) >= req.limit:
                            break
                    _LOG.info(
                        "[OPT_A] find rewrite root=%s name=%r variants=%s "
                        "hits_before=%d hits_after=%d",
                        req.root, name_in, variants, hits_before, len(union),
                    )
                    return self._finish(start, json.dumps({"items": union}),
                                        refs=())
                resp = self._client.find(req)
                if _OPT_A_FIND_CI:
                    _LOG.info(
                        "[OPT_A] find no-rewrite root=%s name=%r hits=%d",
                        req.root, name_in, len(resp.items),
                    )
                return self._finish(start, json.dumps({"items": resp.items}),
                                    refs=())

            if isinstance(req, Req_Search):
                pattern_in = req.pattern
                pattern_out = _maybe_rewrite_ci(pattern_in)
                if _OPT_A_CASE_INSENSITIVE and pattern_out != pattern_in:
                    try:
                        resp_orig = self._client.search(req)
                        hits_before = len(resp_orig.matches)
                    except Exception:
                        hits_before = -1
                    rewritten_req = req.model_copy(update={"pattern": pattern_out})
                    resp = self._client.search(rewritten_req)
                    hits_after = len(resp.matches)
                    _LOG.info(
                        "[OPT_A] search rewrite root=%s orig=%r new=%r hits_before=%d hits_after=%d",
                        req.root, pattern_in, pattern_out, hits_before, hits_after,
                    )
                else:
                    resp = self._client.search(req)
                    if _OPT_A_CASE_INSENSITIVE:
                        _LOG.info(
                            "[OPT_A] search no-rewrite root=%s pattern=%r hits=%d",
                            req.root, pattern_in, len(resp.matches),
                        )
                matches = [{"path": m.path, "line": m.line_number,
                            "line_text": m.snippet} for m in resp.matches]
                return self._finish(
                    start,
                    json.dumps({"total_matches": resp.total_matches,
                                "matches": matches}),
                    refs=(),
                )

            if isinstance(req, Req_Context):
                resp = self._client.context()
                return self._finish(
                    start,
                    json.dumps({"time": resp.time, "unix_time": resp.unix_time}),
                    refs=(),
                )

            # ── preflight tools (filesystem-based) ───────────────────
            if isinstance(req, Req_PreflightSchema):
                schema = discover_schema_from_fs(self._client._root)
                payload = {
                    "summary": schema.summary(),
                    "data": schema.as_data(),
                }
                return self._finish(start, json.dumps(payload), refs=())

            raise TypeError(f"unsupported request type: {type(req).__name__}")

        except Exception as exc:
            wall_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                ok=False, content="", refs=(),
                error=str(exc),
                error_code=self._classify_exception(exc),
                wall_ms=wall_ms,
            )

    # ── terminal submission ──────────────────────────────────────────

    def submit_terminal(self, completion: ReportTaskCompletion) -> ToolResult:
        start = time.monotonic()
        message = re.sub(r'(?m)^/', '', completion.message)
        refs = [r.lstrip("/") for r in completion.grounding_refs]
        self.last_answer = {
            "message": message,
            "outcome": completion.outcome,
            "grounding_refs": refs,
            "outcome_justification": getattr(completion, "outcome_justification", ""),
        }
        self._client.answer(completion)
        return self._finish(
            start, "{}",
            refs=tuple(completion.grounding_refs),
        )

    # ── prepass (identity bootstrap) ─────────────────────────────────

    def run_prepass(self, *, session: Any, trace_writer: Any) -> PrepassResult:
        bootstrap_content: list[str] = []
        schema_content: str | None = None
        pre_cmds = [
            ("tree", Req_Tree(tool="tree", root="/")),
            ("read_agents_md", Req_Read(tool="read", path="AGENTS.md")),
            ("context", Req_Context(tool="context")),
            ("preflight_schema", Req_PreflightSchema(tool="preflight_schema")),
        ]
        for label, req in pre_cmds:
            result = self.dispatch(req)
            if result.ok:
                session.identity_loaded = True
                if label == "read_agents_md":
                    session.rulebook_loaded = True
                for ref in result.refs:
                    session.seen_refs.add(ref)
                if label == "preflight_schema" and result.content:
                    bootstrap_content.append(
                        "WORKSPACE SCHEMA (auto-discovered, use these roots "
                        "when a preflight tool asks for inbox_root / "
                        "entities_root / finance_roots / projects_root):\n"
                        f"{result.content}"
                    )
                    schema_content = result.content
            schema_roots = None
            if label == "preflight_schema" and result.ok and result.content:
                parsed = parse_schema_content(result.content)
                schema_roots = {
                    "projects_root": parsed.projects_root,
                    "finance_roots": list(parsed.finance_roots),
                    "entities_root": parsed.entities_root,
                    "inbox_root": parsed.inbox_root,
                    "outbox_root": parsed.outbox_root,
                }
            trace_writer.append_prepass(
                cmd=label,
                ok=result.ok,
                bytes=result.bytes,
                wall_ms=result.wall_ms,
                error=result.error,
                error_code=result.error_code,
                schema_roots=schema_roots,
            )

        # Phase 2: semantic index — mirrors PcmAdapter.run_prepass.
        parsed_schema = parse_schema_content(schema_content)
        if parsed_schema.entities_root or parsed_schema.projects_root:
            t0 = time.monotonic()
            try:
                digest = build_digest_from_fs(
                    root=self._client._root,
                    entities_root=parsed_schema.entities_root,
                    projects_root=parsed_schema.projects_root,
                )
                si_ok, si_err = True, None
            except Exception as exc:
                digest, si_ok, si_err = "", False, str(exc)
            wall_ms = int((time.monotonic() - t0) * 1000)
            if si_ok and digest:
                bootstrap_content.append(digest)
            trace_writer.append_prepass(
                cmd="preflight_semantic_index",
                ok=si_ok,
                bytes=len(digest or ""),
                wall_ms=wall_ms,
                error=si_err,
                error_code=None if si_ok else "INTERNAL",
                schema_roots=None,
            )

        return PrepassResult(
            bootstrap_content=bootstrap_content,
            schema=parsed_schema,
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _finish(
        self, start: float, text: str, *, refs: tuple[str, ...]
    ) -> ToolResult:
        encoded = text.encode("utf-8", errors="replace")
        original_bytes = len(encoded)
        truncated = False
        if original_bytes > self._max_bytes:
            encoded = encoded[: self._max_bytes]
            text = encoded.decode("utf-8", errors="replace")
            truncated = True
        wall_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(
            ok=True, content=text, refs=refs,
            error=None, error_code=None, wall_ms=wall_ms,
            truncated=truncated,
            original_bytes=original_bytes if truncated else 0,
        )

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        name = type(exc).__name__
        if "Deadline" in name or "Timeout" in name:
            return "RPC_DEADLINE"
        if "Unavailable" in name or "Connection" in name:
            return "RPC_UNAVAILABLE"
        if isinstance(exc, (TypeError, ValueError)):
            return "INVALID_ARG"
        if isinstance(exc, FileNotFoundError):
            return "PCM_ERROR"
        return "UNKNOWN"


# ── Canonical test tasks ─────────────────────────────────────────────

CANONICAL_TASKS = [
    {
        "task_id": "local_t053",
        "workspace": "artifacts/ws_snapshots/t053/run_0/workspace",
        "instruction": (
            "In which projects is the home server involved? "
            "Return only the exact project names, one per line, "
            "sorted alphabetically."
        ),
        "expected_answer": (
            "Hearthline\n"
            "House Mesh\n"
            "Repair Ledger"
        ),
        "context_date": "2026-04-13T10:00:00Z",
    },
    {
        "task_id": "local_t078",
        "workspace": "artifacts/ws_snapshots/t078/run_0/workspace",
        "instruction": (
            "In which projects is my design partner involved? "
            "Return only the exact project names, one per line, "
            "sorted alphabetically."
        ),
        "expected_answer": (
            "Helios Workflow Sprint\n"
            "Northstar Ledger"
        ),
        "context_date": "2026-04-13T10:00:00Z",
    },
    {
        "task_id": "local_t084",
        "workspace": "artifacts/ws_snapshots/t084/run_0/workspace",
        "instruction": (
            '从January 2026开始，我们通过服务项目"operator  workflow '
            'discovery sprint"赚了多少钱？只回答一个数字。'
        ),
        "expected_answer": "650",
        "context_date": "2026-04-13T10:00:00Z",
    },
]


# ── Answer comparison ────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Normalize text for loose comparison."""
    lines = [ln.strip().lower() for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(sorted(lines))


def check_answer(actual: str, expected: str) -> tuple[bool, str]:
    """Compare agent answer to expected. Returns (pass, detail)."""
    if not actual:
        return False, "no answer produced"
    # Exact match (case-insensitive, sorted lines)
    na = _normalize(actual)
    ne = _normalize(expected)
    if na == ne:
        return True, "exact match"
    # Check if expected lines are a subset of actual
    expected_lines = set(ne.splitlines())
    actual_lines = set(na.splitlines())
    if expected_lines <= actual_lines:
        extra = actual_lines - expected_lines
        return True, f"superset match (extra: {extra})"
    missing = expected_lines - actual_lines
    return False, f"missing: {missing}"


def check_outbox_refs(
    client: LocalPcmClient,
    answer: str,
    grounding_refs: list[str],
) -> tuple[bool, str]:
    """Check that outbox attachment paths appear in the answer or grounding_refs.

    Mimics BitGN server scoring: if the agent wrote outbox files with
    attachments, the answer message must reference each attached path.
    Returns (pass, detail). If no outbox writes, returns (True, "").
    """
    import re

    # Find outbox files the agent wrote
    outbox_writes = {
        p: content for p, content in client.writes.items()
        if "outbox/" in p and p.endswith(".md")
    }
    if not outbox_writes:
        return True, ""

    # Extract attachment paths from outbox YAML frontmatter
    required_refs: set[str] = set()
    for path, content in outbox_writes.items():
        # Parse YAML frontmatter between --- delimiters
        m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not m:
            continue
        frontmatter = m.group(1)
        in_attachments = False
        for line in frontmatter.split("\n"):
            stripped = line.strip()
            if stripped.startswith("attachments:"):
                in_attachments = True
                continue
            if in_attachments:
                if stripped.startswith("- "):
                    ref = stripped[2:].strip().strip('"').strip("'")
                    if ref:
                        required_refs.add(ref)
                elif stripped and not stripped.startswith("#"):
                    in_attachments = False

    if not required_refs:
        return True, ""

    # Check: each required ref must appear in answer text or grounding_refs
    answer_lower = answer.lower()
    refs_text = " ".join(grounding_refs).lower()
    combined = answer_lower + " " + refs_text

    missing = set()
    for ref in required_refs:
        if ref.lower() not in combined:
            missing.add(ref)

    if missing:
        return False, f"answer missing required reference(s): {missing}"
    return True, ""


# ── Run a single task ────────────────────────────────────────────────

@dataclass
class LocalTaskResult:
    task_id: str
    passed: bool
    detail: str
    outcome: str
    answer: str
    expected: str
    steps: int
    llm_calls: int
    wall_sec: float


def run_local_task(
    *,
    task_id: str,
    workspace: str | Path,
    instruction: str,
    expected_answer: str,
    expected_outcome: str | None = None,
    context_date: str | None = None,
    cfg: AgentConfig,
    backend: OpenAIChatBackend,
    log_dir: Path,
    router: Any = None,
    reactive_router: Any = None,
) -> LocalTaskResult:
    """Run one task against a local workspace snapshot."""
    import shutil
    import tempfile

    workspace = Path(workspace)
    if not workspace.exists():
        return LocalTaskResult(
            task_id=task_id, passed=False, detail="workspace not found",
            outcome="error", answer="", expected=expected_answer,
            steps=0, llm_calls=0, wall_sec=0,
        )

    # Copy workspace to a temp dir so source snapshot stays clean
    tmp_dir = tempfile.mkdtemp(prefix=f"localbench_{task_id}_")
    tmp_workspace = Path(tmp_dir) / "workspace"
    shutil.copytree(workspace, tmp_workspace)
    workspace = tmp_workspace

    t0 = time.monotonic()
    client = LocalPcmClient(str(workspace), context_date=context_date)

    trace_path = log_dir / f"{task_id}.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    writer = TraceWriter(path=trace_path)
    # Wrap in TracingPcmClient so production adapters (search retry
    # wrapper, pcm_op tracing) are exercised against the local snapshot.
    # Without this, LocalPcmAdapter calls LocalPcmClient directly and
    # bypasses the prod code path.
    from bitgn_contest_agent.adapter.pcm_tracing import TracingPcmClient
    traced_client = TracingPcmClient(client, writer=writer)
    adapter = LocalPcmAdapter(
        client=traced_client, max_tool_result_bytes=cfg.max_tool_result_bytes,
    )
    writer.write_meta(TraceMeta(
        agent_version="local-bench",
        agent_commit="local",
        model=cfg.model,
        backend="openai_compat",
        reasoning_effort=cfg.reasoning_effort,
        benchmark="local",
        task_id=task_id,
        task_index=0,
        started_at=datetime.now(timezone.utc).isoformat(),
        trace_schema_version=TRACE_SCHEMA_VERSION,
        harness_url=None,
        intent_head=instruction[:240],
    ))

    inflight_semaphore = threading.Semaphore(cfg.max_inflight_llm)
    metrics = RunMetrics(max_inflight_llm=cfg.max_inflight_llm)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=cfg.max_steps,
        llm_http_timeout_sec=float(cfg.llm_http_timeout_sec),
        cancel_event=threading.Event(),
        backend_backoff_ms=cfg.rate_limit_backoff_ms,
        inflight_semaphore=inflight_semaphore,
        metrics=metrics,
        router=router,
        reactive_router=reactive_router,
    )

    try:
        result: AgentLoopResult = loop.run(
            task_id=task_id, task_text=instruction,
        )
    except Exception as exc:
        writer.close()
        return LocalTaskResult(
            task_id=task_id, passed=False, detail=f"crash: {exc}",
            outcome="error", answer="", expected=expected_answer,
            steps=0, llm_calls=0, wall_sec=time.monotonic() - t0,
        )

    writer.close()
    wall_sec = time.monotonic() - t0

    # Extract the agent's answer
    answer = ""
    if adapter.last_answer:
        answer = adapter.last_answer.get("message", "")
        outcome = adapter.last_answer.get("outcome", result.terminated_by)
    else:
        outcome = result.terminated_by

    if expected_outcome:
        if outcome == expected_outcome:
            passed, detail = True, f"outcome match ({outcome})"
        else:
            passed, detail = False, f"expected outcome {expected_outcome}, got {outcome}"
    else:
        passed, detail = check_answer(answer, expected_answer)

    # Additional check: outbox attachment references in answer (mimics server)
    if passed:
        grounding = adapter.last_answer.get("grounding_refs", []) if adapter.last_answer else []
        ref_ok, ref_detail = check_outbox_refs(client, answer, grounding)
        if not ref_ok:
            passed = False
            detail = ref_detail

    # Clean up temp workspace
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return LocalTaskResult(
        task_id=task_id,
        passed=passed,
        detail=detail,
        outcome=outcome,
        answer=answer,
        expected=expected_answer,
        steps=result.total_steps,
        llm_calls=result.total_llm_calls,
        wall_sec=wall_sec,
    )


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="local-bench",
        description="Run agent against local workspace snapshots",
    )
    parser.add_argument("--workspace", help="Path to workspace snapshot")
    parser.add_argument("--instruction", help="Task instruction text")
    parser.add_argument("--expected", help="Expected answer text")
    parser.add_argument("--context-date", help="Context date (ISO format)")
    parser.add_argument("--manifest", help="JSON manifest with task list")
    parser.add_argument("--canonical", action="store_true",
                        help="Run the 3 canonical test tasks (t053, t078, t084)")
    parser.add_argument("--snapshots", action="store_true",
                        help="Auto-discover all workspace snapshots with metadata.json")
    parser.add_argument("--log-dir", default="logs/local_bench",
                        help="Trace output directory")
    parser.add_argument("--task-filter", help="Comma-separated task IDs to run")
    args = parser.parse_args()

    # BITGN_API_KEY is not needed for local bench (no PROD server contact)
    import os
    if not os.environ.get("BITGN_API_KEY"):
        os.environ["BITGN_API_KEY"] = "local-bench-dummy"
    cfg = load_from_env()
    backend = OpenAIChatBackend.from_config(
        base_url=cfg.cliproxy_base_url,
        api_key=cfg.cliproxy_api_key,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )

    skills_dir = Path(__file__).resolve().parent.parent / "src" / "bitgn_contest_agent" / "skills"
    router = load_router(skills_dir=skills_dir)
    reactive_dir = skills_dir / "reactive"
    reactive_router = load_reactive_router(reactive_dir)

    log_dir = Path(args.log_dir) / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Build task list
    tasks: list[dict] = []
    if args.canonical:
        tasks = list(CANONICAL_TASKS)
    elif args.snapshots:
        # Auto-discover snapshots with metadata.json (all runs)
        snap_root = _REPO / "artifacts" / "ws_snapshots"
        for snap_dir in sorted(snap_root.iterdir()):
            if not snap_dir.is_dir():
                continue
            for run_dir in sorted(snap_dir.iterdir()):
                if not run_dir.name.startswith("run_"):
                    continue
                meta_file = run_dir / "metadata.json"
                ws_dir = run_dir / "workspace"
                if meta_file.exists() and ws_dir.exists():
                    with open(meta_file) as f:
                        meta = json.load(f)
                    run_suffix = f"_{run_dir.name}" if run_dir.name != "run_0" else ""
                    tasks.append({
                        "task_id": f"local_{snap_dir.name}{run_suffix}",
                        "workspace": str(ws_dir),
                        "instruction": meta["instruction"],
                        "expected_answer": meta.get("expected_answer", ""),
                        "expected_outcome": meta.get("expected_outcome"),
                        "context_date": meta.get("context_date"),
                    })
        if not tasks:
            print("No snapshots with metadata.json found in artifacts/ws_snapshots/")
            return 1
    elif args.manifest:
        with open(args.manifest) as f:
            tasks = json.load(f)
    elif args.workspace and args.instruction:
        tasks = [{
            "task_id": "adhoc",
            "workspace": args.workspace,
            "instruction": args.instruction,
            "expected_answer": args.expected or "",
            "context_date": args.context_date,
        }]
    else:
        parser.error("Specify --canonical, --manifest, or --workspace + --instruction")

    if args.task_filter:
        allowed = set(args.task_filter.split(","))
        tasks = [t for t in tasks if t["task_id"] in allowed]

    if not tasks:
        print("No tasks to run.")
        return 1

    print(f"Running {len(tasks)} task(s), model={cfg.model}, "
          f"reasoning={cfg.reasoning_effort}")
    print(f"Traces → {log_dir}")
    print()

    results: list[LocalTaskResult] = []
    for i, task in enumerate(tasks, 1):
        tid = task["task_id"]
        ws = Path(task["workspace"])
        # Resolve relative paths from repo root
        if not ws.is_absolute():
            ws = _REPO / ws
        print(f"[{i}/{len(tasks)}] {tid}: {task['instruction'][:80]}...")
        r = run_local_task(
            task_id=tid,
            workspace=ws,
            instruction=task["instruction"],
            expected_answer=task.get("expected_answer", ""),
            expected_outcome=task.get("expected_outcome"),
            context_date=task.get("context_date"),
            cfg=cfg,
            backend=backend,
            log_dir=log_dir,
            router=router,
            reactive_router=reactive_router,
        )
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"  {status} | outcome={r.outcome} | steps={r.steps} | "
              f"llm_calls={r.llm_calls} | {r.wall_sec:.1f}s")
        if not r.passed:
            print(f"  detail: {r.detail}")
            if r.answer:
                print(f"  answer:   {r.answer[:200]}")
            print(f"  expected: {r.expected[:200]}")
        print()

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"{'='*60}")
    print(f"Results: {passed}/{total} passed")
    for r in results:
        tag = "PASS" if r.passed else "FAIL"
        print(f"  {r.task_id}: {tag} ({r.detail})")

    # Save results
    results_path = log_dir / "results.json"
    with open(results_path, "w") as f:
        json.dump(
            [{
                "task_id": r.task_id,
                "passed": r.passed,
                "detail": r.detail,
                "outcome": r.outcome,
                "answer": r.answer,
                "expected": r.expected,
                "steps": r.steps,
                "llm_calls": r.llm_calls,
                "wall_sec": round(r.wall_sec, 1),
            } for r in results],
            f, indent=2, ensure_ascii=False,
        )
    print(f"Results → {results_path}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
