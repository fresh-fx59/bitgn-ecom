"""§5.2 Test 4 — analyzer completeness over the full trace variant space."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from bitgn_contest_agent.schemas import REQ_MODELS
from bitgn_contest_agent.trace_schema import (
    ERROR_CODE_VALUES,
    ERROR_KIND_VALUES,
    EVENT_KIND_VALUES,
    TERMINATED_BY_VALUES,
    TRACE_SCHEMA_VERSION,
    load_jsonl,
)
from scripts.bench_summary import FROZEN_SCHEMA_KEYS, summarize


def _synth_trace_for_task(path: Path, *, task_id: str, outcome: str) -> None:
    """Produce a trace that exercises every Req_* variant as a step and
    every event kind + error code as events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append(
        json.dumps(
            {
                "kind": "meta",
                "agent_version": "0.0.7",
                "agent_commit": "x",
                "model": "gpt-5.3-codex",
                "backend": "openai_compat",
                "reasoning_effort": "medium",
                "benchmark": "bitgn/pac1-dev",
                "task_id": task_id,
                "task_index": 0,
                "started_at": "2026-04-10T00:00:00Z",
                "trace_schema_version": TRACE_SCHEMA_VERSION,
            }
        )
    )
    lines.append(json.dumps({"kind": "task", "task_id": task_id, "task_text": "x"}))
    for i, model in enumerate(REQ_MODELS, start=1):
        lines.append(
            json.dumps(
                {
                    "kind": "step",
                    "step": i,
                    "wall_ms": 10,
                    "llm": {
                        "latency_ms": 10,
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "cached_tokens": 0,
                        "retry_count": 0,
                    },
                    "next_step": {"tool": get_args(model.model_fields["tool"].annotation)[0]},
                    "tool_result": {
                        "ok": True,
                        "bytes": 1,
                        "wall_ms": 1,
                        "truncated": False,
                        "original_bytes": 0,
                        "error": None,
                        "error_code": None,
                    },
                    "session_after": {"seen_refs_count": i, "identity_loaded": True, "rulebook_loaded": True},
                }
            )
        )
    for ek in sorted(EVENT_KIND_VALUES):
        lines.append(
            json.dumps({"kind": "event", "at_step": 1, "event_kind": ek})
        )
    lines.append(
        json.dumps(
            {
                "kind": "outcome",
                "terminated_by": "report_completion",
                "reported": outcome,
                "enforcer_bypassed": False,
                "error_kind": None,
                "total_steps": len(REQ_MODELS),
                "total_llm_calls": len(REQ_MODELS),
                "total_prompt_tokens": len(REQ_MODELS),
                "total_completion_tokens": len(REQ_MODELS),
                "total_cached_tokens": 0,
                "score": 1.0 if outcome == "OUTCOME_OK" else 0.0,
            }
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_every_req_variant_is_parseable_by_load_jsonl(tmp_path: Path) -> None:
    trace = tmp_path / "t1__run0.jsonl"
    _synth_trace_for_task(trace, task_id="t1", outcome="OUTCOME_OK")
    records = list(load_jsonl(trace))
    # meta + task + N steps + K events + outcome
    assert records[0].kind == "meta"
    assert records[1].kind == "task"
    assert records[-1].kind == "outcome"
    step_count = sum(1 for r in records if r.kind == "step")
    assert step_count == len(REQ_MODELS)


def test_summary_keys_match_frozen_schema_over_exhaustive_synthetic_trace(tmp_path: Path) -> None:
    _synth_trace_for_task(tmp_path / "t1__run0.jsonl", task_id="t1", outcome="OUTCOME_OK")
    _synth_trace_for_task(tmp_path / "t2__run0.jsonl", task_id="t2", outcome="OUTCOME_NONE_CLARIFICATION")
    summary = summarize(logs_dir=tmp_path)
    assert set(summary.keys()) == set(FROZEN_SCHEMA_KEYS)
    assert "t1" in summary["tasks"]
    assert "t2" in summary["tasks"]
    assert summary["overall"]["total_runs"] == 2


def test_closed_enum_sets_are_non_empty_and_disjoint_where_expected() -> None:
    assert EVENT_KIND_VALUES
    assert TERMINATED_BY_VALUES
    assert ERROR_CODE_VALUES
    assert ERROR_KIND_VALUES
    # terminated_by and event_kind must not overlap — they live in different enum slots.
    assert TERMINATED_BY_VALUES.isdisjoint(EVENT_KIND_VALUES)
