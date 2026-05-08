"""failure_report pcm_op aggregation — appends op counts and top-5
expensive ops to each failed task's digest so a trace sampler doesn't
need a second tool to see what the agent actually did under the hood.
"""
from __future__ import annotations

import json
from pathlib import Path

from bitgn_contest_agent.trace_schema import (
    TRACE_SCHEMA_VERSION,
    TraceMeta,
    TraceOutcome,
)
from bitgn_contest_agent.trace_writer import TraceWriter

from scripts.failure_report import _digest_trace, _render_json, _render_md


def _write_failed_trace_with_pcm_ops(
    path: Path, task_id: str, ops: list[dict]
) -> None:
    w = TraceWriter(path=path)
    w.write_meta(TraceMeta(
        agent_version="x", agent_commit="abc123", model="gpt-5.3-codex",
        backend="openai_compat", reasoning_effort="medium",
        benchmark="bench", task_id=task_id, task_index=0,
        started_at="2026-04-16T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
        intent_head="q",
    ))
    for op in ops:
        w.append_pcm_op(
            op=op["op"], path=op.get("path"), bytes=op.get("bytes", 0),
            wall_ms=op.get("wall_ms", 0), ok=op.get("ok", True),
            error_code=op.get("error_code"), origin=op.get("origin"),
        )
    w.append_outcome(TraceOutcome(
        terminated_by="report_completion",
        reported="OUTCOME_NONE_CLARIFICATION",
        enforcer_bypassed=False, total_steps=3, total_llm_calls=3,
        total_prompt_tokens=100, total_completion_tokens=50,
        score=0.0,
    ))
    w.close()


def test_digest_captures_pcm_op_counts_by_origin(tmp_path: Path) -> None:
    """Origin buckets (prepass/routed_preflight/step/other) match
    bench_summary's collapsed scheme so the two reports agree."""
    ops = [
        {"op": "tree", "path": "/", "wall_ms": 100, "origin": "prepass"},
        {"op": "read", "path": "AGENTS.md", "wall_ms": 50, "origin": "prepass"},
        {"op": "read", "path": "x.md", "wall_ms": 80, "origin": "routed_preflight"},
        {"op": "read", "path": "hanna.md", "wall_ms": 40, "origin": "step:1"},
        {"op": "answer", "wall_ms": 30, "origin": "step:2"},
        {"op": "read", "path": "legacy.md", "wall_ms": 10, "origin": None},
    ]
    p = tmp_path / "t033__run0.jsonl"
    _write_failed_trace_with_pcm_ops(p, "t033", ops)

    d = _digest_trace(p)
    assert d is not None
    assert d.pcm_ops_total == 6
    assert d.pcm_ops_by_origin == {
        "prepass": 2,
        "routed_preflight": 1,
        "step": 2,
        "other": 1,
    }


def test_digest_captures_top_expensive_pcm_ops(tmp_path: Path) -> None:
    """Top-N list ranks by wall_ms descending; when there are more
    than N ops we keep only the most expensive — the slow ops are the
    ones worth looking at during triage."""
    ops = [
        {"op": "read", "path": f"f{i}.md", "wall_ms": i, "origin": "step:1"}
        for i in range(1, 10)
    ]
    # Add a clearly-slow op to verify ranking.
    ops.append({"op": "read", "path": "slow.md", "wall_ms": 9999,
                "origin": "prepass"})
    p = tmp_path / "t033__run0.jsonl"
    _write_failed_trace_with_pcm_ops(p, "t033", ops)

    d = _digest_trace(p)
    assert d is not None
    assert len(d.pcm_top_ops) == 5
    # Slowest op first.
    assert d.pcm_top_ops[0].wall_ms == 9999
    assert d.pcm_top_ops[0].path == "slow.md"
    # Second-slowest: wall_ms=9 (f9.md).
    assert d.pcm_top_ops[1].wall_ms == 9
    assert d.pcm_top_ops[1].path == "f9.md"


def test_render_md_includes_pcm_op_breakdown(tmp_path: Path) -> None:
    ops = [
        {"op": "tree", "path": "/", "wall_ms": 200, "origin": "prepass"},
        {"op": "read", "path": "AGENTS.md", "wall_ms": 100, "origin": "prepass"},
        {"op": "read", "path": "hanna.md", "wall_ms": 50, "origin": "step:1"},
    ]
    p = tmp_path / "t033__run0.jsonl"
    _write_failed_trace_with_pcm_ops(p, "t033", ops)
    d = _digest_trace(p)
    md = _render_md(d)

    # Count line with origin split
    assert "pcm_ops" in md
    assert "3" in md  # total
    assert "prepass=2" in md
    assert "step=1" in md
    # Top ops section lists the slowest by wall_ms
    assert "top pcm_ops" in md or "top ops" in md
    assert "tree" in md
    assert "200" in md  # the slowest op's wall_ms
    assert "prepass" in md


def test_render_md_omits_pcm_section_when_no_ops(tmp_path: Path) -> None:
    """Traces written before pcm_op tracing landed have 0 ops —
    suppressing the section keeps old triage output readable."""
    p = tmp_path / "t033__run0.jsonl"
    _write_failed_trace_with_pcm_ops(p, "t033", ops=[])

    d = _digest_trace(p)
    md = _render_md(d)
    assert "pcm_ops" not in md
    assert "top ops" not in md


def test_render_json_includes_pcm_section(tmp_path: Path) -> None:
    """JSON output carries the same pcm_ops fields as markdown so
    downstream tooling doesn't have to branch on format."""
    ops = [
        {"op": "tree", "path": "/", "wall_ms": 200, "origin": "prepass"},
        {"op": "read", "path": "hanna.md", "wall_ms": 50, "origin": "step:1"},
    ]
    p = tmp_path / "t033__run0.jsonl"
    _write_failed_trace_with_pcm_ops(p, "t033", ops)
    d = _digest_trace(p)

    payload = json.loads(_render_json([d]))
    assert len(payload) == 1
    pcm = payload[0]["pcm_ops"]
    assert pcm["total"] == 2
    assert pcm["by_origin"] == {"prepass": 1, "step": 1}
    assert len(pcm["top"]) == 2
    assert pcm["top"][0]["path"] == "/"
    assert pcm["top"][0]["wall_ms"] == 200
    assert pcm["top"][0]["origin"] == "prepass"
