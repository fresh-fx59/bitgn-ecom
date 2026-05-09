"""Minimal bench summary for ECOM runs.

The PAC1 lineage shipped a much richer ``scripts/bench_summary.py``
that computed per-task aggregates, op-budget breakdowns, divergence
counts, and token usage. That module was deleted along with the rest of
``scripts/`` during the ECOM port (it consumed PAC1-specific JSON
shapes); a richer ECOM summary can be re-authored once enough run
evidence accumulates.

For now this module exposes the two names ``cli.py`` imports —
``summarize`` and ``load_summary`` — backed by a small but useful
walker over the run directory. The output captures everything the
operator usually wants on a one-shot run:

  - per-task: terminated_by, outcome (`reported`), score, score_detail,
    total steps / LLM calls / token counts, error_kind / error_msg,
    harness_url
  - overall: task count, OUTCOME_OK count, mean score (when scores are
    available), total LLM calls, total prompt/completion tokens

Consumers that read older v1.x summaries continue to work because
``load_summary`` tolerates unknown keys.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


BENCH_SUMMARY_SCHEMA_VERSION = "ecom-0.1.0"
FROZEN_SCHEMA_KEYS = ("schema_version", "overall", "tasks")


def _iter_trace_files(logs_dir: Path) -> Iterable[Path]:
    """Yield each per-trial JSONL trace under ``logs_dir``.

    The CLI writes traces as ``<task_id>__run<N>.jsonl`` directly in
    ``logs_dir`` (the run directory). Anything else in the dir is
    ignored (e.g. ``.log`` files from the file-handler logger).
    """
    if not logs_dir.exists() or not logs_dir.is_dir():
        return
    for path in sorted(logs_dir.iterdir()):
        if path.is_file() and path.name.endswith(".jsonl"):
            yield path


def _summarize_trace(path: Path) -> Dict[str, Any]:
    """Walk one trace, return a per-task record. Best-effort: any
    parse failure on a single line is skipped, not propagated. Pulls:
      - meta (task_id, harness_url, model, started_at)
      - outcome (terminated_by, reported, score, score_detail, error_*,
        total_steps / total_llm_calls / token totals)
    """
    rec: Dict[str, Any] = {
        "task_id": path.stem.split("__")[0],
        "trace_file": path.name,
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        kind = obj.get("kind")
        if kind == "meta":
            for key in (
                "task_id", "harness_url", "model", "backend",
                "reasoning_effort", "started_at",
            ):
                if obj.get(key) is not None:
                    rec[key] = obj[key]
        elif kind == "outcome":
            for key in (
                "terminated_by", "reported", "score", "score_detail",
                "error_kind", "error_msg", "enforcer_bypassed",
                "total_steps", "total_llm_calls", "total_prompt_tokens",
                "total_completion_tokens", "total_cached_tokens",
                "total_reasoning_tokens",
            ):
                if obj.get(key) is not None:
                    rec[key] = obj[key]
    return rec


def _overall(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(tasks)
    ok = sum(1 for t in tasks if t.get("reported") == "OUTCOME_OK")
    scores = [t["score"] for t in tasks if isinstance(t.get("score"), (int, float))]
    return {
        "task_count": n,
        "outcome_ok_count": ok,
        "scored_count": len(scores),
        "mean_score": (sum(scores) / len(scores)) if scores else None,
        "total_llm_calls": sum(t.get("total_llm_calls") or 0 for t in tasks),
        "total_prompt_tokens": sum(t.get("total_prompt_tokens") or 0 for t in tasks),
        "total_completion_tokens": sum(t.get("total_completion_tokens") or 0 for t in tasks),
        "total_steps": sum(t.get("total_steps") or 0 for t in tasks),
    }


def summarize(*, logs_dir: Path) -> Dict[str, Any]:
    """Walk ``logs_dir`` and return the bench summary object the CLI
    serializes to ``--output``."""
    tasks = [_summarize_trace(p) for p in _iter_trace_files(Path(logs_dir))]
    return {
        "schema_version": BENCH_SUMMARY_SCHEMA_VERSION,
        "overall": _overall(tasks),
        "tasks": tasks,
    }


def load_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compat shim used by cli.py for triage display.

    Older PAC1 summaries had richer shapes; this implementation just
    fills in the keys the rest of the CLI depends on (``overall`` /
    ``tasks``) and tolerates unknown keys passed in.
    """
    out: Dict[str, Any] = {
        "schema_version": raw.get("schema_version", BENCH_SUMMARY_SCHEMA_VERSION),
        "overall": dict(raw.get("overall") or {}),
        "tasks": list(raw.get("tasks") or []),
    }
    return out
