# tests/test_arch_report.py
"""arch_report CLI: timeline + filtering by enum-typed args."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bitgn_contest_agent.arch_constants import (
    ArchCategory, ValidatorT1Rule, ValidatorT2Trigger,
)
from bitgn_contest_agent.arch_log import emit_arch
from bitgn_contest_agent.trace_schema import (
    TraceMeta, TraceOutcome, TRACE_SCHEMA_VERSION,
)
from bitgn_contest_agent.trace_writer import TraceWriter


def _make_trace(path: Path, task_id: str, intent: str) -> None:
    writer = TraceWriter(path=path)
    writer.write_meta(TraceMeta(
        agent_version="x", agent_commit="y", model="m", backend="b",
        reasoning_effort="medium", benchmark="bench",
        task_id=task_id, task_index=0,
        started_at="2026-04-14T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
        intent_head=intent,
    ))
    emit_arch(writer, category=ArchCategory.TASK_START, details=intent)
    emit_arch(writer, category=ArchCategory.SKILL_ROUTER,
              skill="finance-lookup", confidence=0.9)
    emit_arch(writer, category=ArchCategory.VALIDATOR_T1,
              at_step=2, rule=ValidatorT1Rule.MUTATION_GUARD)
    emit_arch(writer, category=ArchCategory.VALIDATOR_T2,
              at_step=5, trigger=ValidatorT2Trigger.FIRST_TRANSITION)
    writer.append_outcome(TraceOutcome(
        terminated_by="report_completion", reported="OUTCOME_OK",
        enforcer_bypassed=False, total_steps=1, total_llm_calls=1,
        total_prompt_tokens=0, total_completion_tokens=0, score=1.0,
    ))
    writer.close()


def _run_script(*args: str) -> tuple[int, str]:
    repo_root = Path(__file__).parent.parent
    script = repo_root / "scripts" / "arch_report.py"
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, cwd=repo_root,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_single_task_timeline(tmp_path) -> None:
    path = tmp_path / "t100__run0.jsonl"
    _make_trace(path, "t100", "how much did I pay?")
    rc, out = _run_script(str(path))
    assert rc == 0
    assert "t100" in out
    assert "SKILL_ROUTER" in out
    assert "VALIDATOR_T1" in out
    assert "VALIDATOR_T2" in out


def test_filter_by_category(tmp_path) -> None:
    path = tmp_path / "t100__run0.jsonl"
    _make_trace(path, "t100", "q")
    rc, out = _run_script(str(path), "--category", "VALIDATOR_T2")
    assert rc == 0
    assert "VALIDATOR_T2" in out
    assert "VALIDATOR_T1" not in out
    assert "SKILL_ROUTER" not in out


def test_filter_by_trigger(tmp_path) -> None:
    path = tmp_path / "t100__run0.jsonl"
    _make_trace(path, "t100", "q")
    rc, out = _run_script(
        str(path), "--category", "VALIDATOR_T2",
        "--trigger", "first_transition",
    )
    assert rc == 0
    assert "first_transition" in out


def test_filter_by_invalid_category_fails_argparse(tmp_path) -> None:
    path = tmp_path / "t100__run0.jsonl"
    _make_trace(path, "t100", "q")
    rc, out = _run_script(str(path), "--category", "NOT_A_CATEGORY")
    assert rc != 0
    assert "NOT_A_CATEGORY" in out or "invalid choice" in out.lower()


def test_run_dir_lists_all_tasks(tmp_path) -> None:
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _make_trace(run_dir / "t100__run0.jsonl", "t100", "intent 100")
    _make_trace(run_dir / "t101__run0.jsonl", "t101", "intent 101")
    rc, out = _run_script(str(run_dir))
    assert rc == 0
    assert "t100" in out
    assert "t101" in out


def test_filter_by_task_id(tmp_path) -> None:
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _make_trace(run_dir / "t100__run0.jsonl", "t100", "q1")
    _make_trace(run_dir / "t101__run0.jsonl", "t101", "q2")
    rc, out = _run_script(str(run_dir), "--task", "t100")
    assert rc == 0
    assert "t100" in out
    assert "t101" not in out


def _make_trace_with_pcm_ops(path: Path, task_id: str) -> None:
    """Trace mixing arch events with pcm_ops to exercise --include-pcm."""
    writer = TraceWriter(path=path)
    writer.write_meta(TraceMeta(
        agent_version="x", agent_commit="y", model="m", backend="b",
        reasoning_effort="medium", benchmark="bench",
        task_id=task_id, task_index=0,
        started_at="2026-04-16T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
        intent_head="q",
    ))
    emit_arch(writer, category=ArchCategory.TASK_START, details="q")
    writer.append_pcm_op(
        op="tree", path="/", bytes=100, wall_ms=50, ok=True,
        error_code=None, origin="prepass",
    )
    writer.append_pcm_op(
        op="read", path="AGENTS.md", bytes=1800, wall_ms=90, ok=True,
        error_code=None, origin="prepass",
    )
    emit_arch(writer, category=ArchCategory.SKILL_ROUTER,
              skill="finance-lookup", confidence=0.9)
    writer.append_pcm_op(
        op="read", path="hanna.md", bytes=400, wall_ms=40, ok=True,
        error_code=None, origin="step:1",
    )
    emit_arch(writer, category=ArchCategory.VALIDATOR_T1,
              at_step=1, rule=ValidatorT1Rule.MUTATION_GUARD)
    writer.append_outcome(TraceOutcome(
        terminated_by="report_completion", reported="OUTCOME_OK",
        enforcer_bypassed=False, total_steps=1, total_llm_calls=1,
        total_prompt_tokens=0, total_completion_tokens=0, score=1.0,
    ))
    writer.close()


def test_include_pcm_interleaves_pcm_ops_with_arch(tmp_path) -> None:
    """--include-pcm mixes pcm_op rows into the timeline in chronological
    (JSONL-order) position, tagged with op/path/origin so "what PCM call
    fired between the router and the T1 validator?" is answerable from
    the timeline alone."""
    path = tmp_path / "t100__run0.jsonl"
    _make_trace_with_pcm_ops(path, "t100")
    rc, out = _run_script(str(path), "--include-pcm")
    assert rc == 0
    # Arch events still present
    assert "SKILL_ROUTER" in out
    assert "VALIDATOR_T1" in out
    # pcm_ops now shown with op name and origin
    assert "PCM_OP" in out
    assert "op=tree" in out
    assert "op=read" in out
    assert "origin=prepass" in out
    assert "origin=step:1" in out
    # Ordering: the prepass tree read happens BEFORE the skill_router
    # arch event (same JSONL order). Verify by column positions.
    tree_idx = out.find("op=tree")
    router_idx = out.find("SKILL_ROUTER")
    step_read_idx = out.find("origin=step:1")
    t1_idx = out.find("VALIDATOR_T1")
    assert 0 <= tree_idx < router_idx < step_read_idx < t1_idx


def test_default_does_not_show_pcm_ops(tmp_path) -> None:
    """Without --include-pcm, timeline keeps its arch-only shape so
    long-running arch triage isn't drowned in 100+ pcm_op rows."""
    path = tmp_path / "t100__run0.jsonl"
    _make_trace_with_pcm_ops(path, "t100")
    rc, out = _run_script(str(path))
    assert rc == 0
    assert "SKILL_ROUTER" in out
    assert "PCM_OP" not in out
    assert "op=tree" not in out
