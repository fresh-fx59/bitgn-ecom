"""Pair canonical JSON traces with their iterations JSONL sidecar.

Scope: recent-format only. Older traces without ``iteration_log_file`` are
ignored. See ``README.md`` for the rationale.

This module is a library. Use ``bucket_failures.py`` or ``rule_evaluator.py``
as the CLI entry points.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# Repo-relative. Scripts run from repo root.
LOG_ROOT = "task-t01-t43-logs-produced-by-bitgn-contest-agent"


@dataclass
class Run:
    """One paired (canonical JSON, iterations JSONL) run with its grade.

    Only fields that the recent schema reliably exposes are surfaced. Anything
    else stays in ``raw`` for ad-hoc probes.
    """

    task_id: str
    score: float
    outcome: str | None
    grounding_refs: list[str]
    tools_called: set[str]
    step_count: int
    tool_call_count: int

    # Final workflow_state snapshot (from the last JSONL row)
    terminal_mode: str | None
    current_phase: str | None
    task_family: str | None
    family_confidence: float | None
    finalization_ready: bool | None
    terminal_readiness_ready: bool | None
    respond_instructions_loaded: bool | None  # from verification_status
    post_mutation_verification: bool | None
    has_mutation: bool | None
    read_count: int
    write_count: int
    delete_count: int
    discovery_count: int
    grounded_paths_n: int
    forced_fallback: bool | None
    repeat_count: int
    must_switch_strategy: bool | None
    no_new_evidence: bool | None
    required_obligations_pending: list[str]

    canonical_path: str
    jsonl_path: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


# Read-like tools used by the nontrivial-work gate. Names match what the
# sibling agent actually calls in the traces (starts with /fs/).
READ_LIKE_TOOLS: frozenset[str] = frozenset(
    {"/fs/read", "/fs/list", "/fs/search", "/fs/tree", "/fs/find", "/fs/outline"}
)

# Identity/context tools that the pre-execution identity pass uses.
IDENTITY_TOOLS: frozenset[str] = frozenset(
    {"/fs/context", "/load-respond-instructions", "/fs/read"}
)

# Refusal outcomes exempt from identity/nontrivial-work gates — it is
# legitimate to refuse from the task description alone.
REFUSAL_OUTCOMES: frozenset[str] = frozenset(
    {"OUTCOME_NONE_UNSUPPORTED", "OUTCOME_DENIED_SECURITY"}
)


def _candidate_jsonl_paths(canonical_json_path: str, iter_file: str) -> list[str]:
    """JSONL sidecars can live in the global iterations/ dir or alongside
    the canonical JSON (for rerun batches)."""

    return [
        os.path.join(LOG_ROOT, "iterations", iter_file),
        os.path.join(os.path.dirname(canonical_json_path), "iterations", iter_file),
    ]


def _iter_canonical_files() -> Iterator[str]:
    """Yield every top-level and rerun canonical JSON path."""

    for name in sorted(os.listdir(LOG_ROOT)):
        if name.endswith(".json") and os.path.isfile(os.path.join(LOG_ROOT, name)):
            yield os.path.join(LOG_ROOT, name)
    for p in sorted(glob.glob(os.path.join(LOG_ROOT, "reruns", "*", "*.json"))):
        yield p


def _extract_task_id(canon: dict[str, Any], jsonl_path: str) -> str | None:
    task = canon.get("task")
    if isinstance(task, dict):
        tid = task.get("task_id") or task.get("taskid")
        if tid:
            return tid
    # fall back to filename: ...taskid_t12.jsonl
    base = os.path.basename(jsonl_path)
    if "taskid_" in base:
        rest = base.split("taskid_", 1)[1]
        return rest.split(".", 1)[0] or None
    return None


def _build_run(
    canonical_path: str,
    canon: dict[str, Any],
    jsonl_path: str,
    rows: list[dict[str, Any]],
) -> Run | None:
    if not rows:
        return None

    last = rows[-1]
    ws = last.get("workflow_state") or {}
    vs = ws.get("verification_status") or {}
    ei = ws.get("evidence_inventory") or {}
    tr = ws.get("terminal_readiness") or {}
    lr = ws.get("loop_risk") or {}
    pls = ws.get("planner_loop_state") or {}

    last_args = last.get("tool_arguments") or {}
    resp = canon.get("response") or {}
    outcome = last_args.get("outcome") or resp.get("outcome")
    grounding = last_args.get("grounding_refs") or resp.get("grounding_refs") or []

    tools_called: set[str] = set()
    tool_call_count = 0
    for r in rows:
        tn = r.get("tool_name")
        if tn:
            tools_called.add(tn)
            tool_call_count += 1

    tid = _extract_task_id(canon, jsonl_path)
    if tid is None:
        return None

    return Run(
        task_id=tid,
        score=float(canon.get("score") or 0.0),
        outcome=outcome,
        grounding_refs=list(grounding) if isinstance(grounding, list) else [],
        tools_called=tools_called,
        step_count=int(last.get("step") or 0),
        tool_call_count=tool_call_count,
        terminal_mode=ws.get("terminal_mode"),
        current_phase=ws.get("current_phase"),
        task_family=ws.get("task_family"),
        family_confidence=ws.get("family_confidence"),
        finalization_ready=ws.get("finalization_ready"),
        terminal_readiness_ready=tr.get("ready"),
        respond_instructions_loaded=vs.get("respond_instructions_loaded"),
        post_mutation_verification=vs.get("post_mutation_verification"),
        has_mutation=vs.get("has_mutation"),
        read_count=int(ei.get("read_count") or 0),
        write_count=int(ei.get("write_count") or 0),
        delete_count=int(ei.get("delete_count") or 0),
        discovery_count=int(ei.get("discovery_count") or 0),
        grounded_paths_n=len(ei.get("grounded_paths") or []),
        forced_fallback=lr.get("forced_fallback"),
        repeat_count=int(pls.get("repeat_count") or 0),
        must_switch_strategy=pls.get("must_switch_strategy"),
        no_new_evidence=pls.get("no_new_evidence"),
        required_obligations_pending=list(ws.get("required_obligations_pending") or []),
        canonical_path=canonical_path,
        jsonl_path=jsonl_path,
        raw={"canon": canon, "last_row": last},
    )


def load_runs() -> list[Run]:
    """Return every paired recent-format run with a non-null score."""

    runs: list[Run] = []
    for canon_path in _iter_canonical_files():
        try:
            with open(canon_path) as f:
                canon = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(canon, dict):
            continue
        ilf = canon.get("iteration_log_file")
        if not (isinstance(ilf, str) and ilf.endswith(".jsonl")):
            continue
        if canon.get("score") is None:
            continue

        jsonl_path: str | None = None
        for c in _candidate_jsonl_paths(canon_path, ilf):
            if os.path.isfile(c):
                jsonl_path = c
                break
        if jsonl_path is None:
            continue

        try:
            with open(jsonl_path) as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except (OSError, json.JSONDecodeError):
            continue

        run = _build_run(canon_path, canon, jsonl_path, rows)
        if run is not None:
            runs.append(run)

    return runs


def partition(runs: Iterable[Run]) -> tuple[list[Run], list[Run]]:
    passed: list[Run] = []
    failed: list[Run] = []
    for r in runs:
        (passed if r.score >= 1.0 else failed).append(r)
    return passed, failed
