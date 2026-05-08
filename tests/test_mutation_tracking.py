"""Verify agent loop records mutations in session."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence
from unittest.mock import MagicMock

from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.backend.base import Backend, Message, NextStepResult
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_step(function: dict, *, observation: str = "obs", outcome_leaning: str = "GATHERING_INFORMATION") -> NextStep:
    return NextStep(
        current_state="x",
        plan_remaining_steps_brief=["do", "report"],
        identity_verified=True,
        observation=observation,
        outcome_leaning=outcome_leaning,
        function=function,
    )


def _wrap(step: NextStep) -> NextStepResult:
    return NextStepResult(parsed=step, prompt_tokens=0, completion_tokens=0, reasoning_tokens=0)


class _ScriptedBackend(Backend):
    def __init__(self, scripted: list[NextStepResult]) -> None:
        self._steps = list(scripted)

    def next_step(self, messages: Sequence[Message], response_schema, timeout_sec):
        return self._steps.pop(0)


def _mk_writer(tmp_path: Path) -> TraceWriter:
    w = TraceWriter(path=tmp_path / "trace.jsonl")
    w.write_meta(
        TraceMeta(
            agent_version="0.0.7", agent_commit="t", model="gpt-5.3-codex",
            backend="openai_compat", reasoning_effort="medium",
            benchmark="bitgn/pac1-dev", task_id="t1", task_index=0,
            started_at="2026-04-10T00:00:00Z",
            trace_schema_version=TRACE_SCHEMA_VERSION,
        )
    )
    return w


def _fake_prepass(session: Session) -> None:
    session.identity_loaded = True
    session.rulebook_loaded = True
    session.seen_refs.add("AGENTS.md")


def test_write_and_delete_recorded_in_session_mutations(tmp_path: Path) -> None:
    """Agent dispatches write + delete; both appear in trace mutation_count."""
    backend = _ScriptedBackend([
        _wrap(_mk_step(
            {"tool": "write", "path": "outbox/reply.md", "content": "hello"},
            observation="wrote reply", outcome_leaning="OUTCOME_OK",
        )),
        _wrap(_mk_step(
            {"tool": "delete", "path": "50_finance/receipt.md"},
            observation="deleted receipt", outcome_leaning="OUTCOME_OK",
        )),
        _wrap(_mk_step(
            {
                "tool": "report_completion", "message": "done",
                "grounding_refs": ["AGENTS.md"],
                "rulebook_notes": "n", "outcome_justification": "j",
                "completed_steps_laconic": ["wrote reply", "deleted receipt"],
                "outcome": "OUTCOME_OK",
            },
            observation="done", outcome_leaning="OUTCOME_OK",
        )),
    ])
    adapter = MagicMock(spec=PcmAdapter)
    adapter.run_prepass = MagicMock(side_effect=lambda *, session, trace_writer: _fake_prepass(session))
    adapter.dispatch.return_value = ToolResult(
        ok=True, content="ok", refs=(), error=None, error_code=None, wall_ms=5,
    )
    adapter.submit_terminal.return_value = ToolResult(
        ok=True, content="", refs=(), error=None, error_code=None, wall_ms=3,
    )
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend, adapter=adapter, writer=writer,
        max_steps=10, llm_http_timeout_sec=30.0,
    )
    result = loop.run(task_id="t1", task_text="do it")
    writer.close()

    assert result.terminated_by == "report_completion"
    # Verify trace contains mutation_count in session_after
    trace_path = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    step_records = [r for r in records if r.get("kind") == "step"]
    # After the write step, mutation_count should be 1
    assert step_records[0]["session_after"]["mutation_count"] == 1
    # After the delete step, mutation_count should be 2
    assert step_records[1]["session_after"]["mutation_count"] == 2


def test_failed_dispatch_not_recorded_as_mutation(tmp_path: Path) -> None:
    """A failed write should NOT be recorded in mutations."""
    backend = _ScriptedBackend([
        _wrap(_mk_step(
            {"tool": "write", "path": "outbox/fail.md", "content": "x"},
            observation="tried to write", outcome_leaning="OUTCOME_OK",
        )),
        _wrap(_mk_step(
            {
                "tool": "report_completion", "message": "done",
                "grounding_refs": ["AGENTS.md"],
                "rulebook_notes": "n", "outcome_justification": "j",
                "completed_steps_laconic": ["attempted write"],
                "outcome": "OUTCOME_OK",
            },
            observation="done", outcome_leaning="OUTCOME_OK",
        )),
    ])
    adapter = MagicMock(spec=PcmAdapter)
    adapter.run_prepass = MagicMock(side_effect=lambda *, session, trace_writer: _fake_prepass(session))
    # First dispatch FAILS, terminal succeeds.
    adapter.dispatch.return_value = ToolResult(
        ok=False, content="", refs=(), error="permission denied", error_code="EPERM", wall_ms=5,
    )
    adapter.submit_terminal.return_value = ToolResult(
        ok=True, content="", refs=(), error=None, error_code=None, wall_ms=3,
    )
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend, adapter=adapter, writer=writer,
        max_steps=10, llm_http_timeout_sec=30.0,
    )
    result = loop.run(task_id="t1", task_text="do it")
    writer.close()

    trace_path = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    step_records = [r for r in records if r.get("kind") == "step"]
    assert step_records[0]["session_after"]["mutation_count"] == 0
