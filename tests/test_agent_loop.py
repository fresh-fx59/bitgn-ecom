"""Agent loop scaffold — happy path + enforcer retry path."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.agent import AgentLoop, AgentLoopResult
from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.backend.base import Backend, Message, NextStepResult, TransientBackendError
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session
from bitgn_contest_agent.trace_schema import TRACE_SCHEMA_VERSION, TraceMeta
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_step(
    function: dict,
    *,
    observation: str = "step observation",
    outcome_leaning: str = "GATHERING_INFORMATION",
) -> NextStep:
    return NextStep(
        current_state="x",
        plan_remaining_steps_brief=["do", "report"],
        identity_verified=True,
        observation=observation,
        outcome_leaning=outcome_leaning,
        function=function,
    )


def _wrap(step: NextStep) -> NextStepResult:
    """Wrap a NextStep with zero tokens for backward compatibility."""
    return NextStepResult(parsed=step, prompt_tokens=0, completion_tokens=0, reasoning_tokens=0)


class _ScriptedBackend(Backend):
    def __init__(self, scripted: list[NextStepResult]) -> None:
        self._steps = list(scripted)
        self.calls = 0

    def next_step(self, messages: Sequence[Message], response_schema, timeout_sec):  # type: ignore[override]
        self.calls += 1
        return self._steps.pop(0)


def _mk_writer(tmp_path: Path) -> TraceWriter:
    w = TraceWriter(path=tmp_path / "trace.jsonl")
    w.write_meta(
        TraceMeta(
            agent_version="0.0.7",
            agent_commit="t",
            model="gpt-5.3-codex",
            backend="openai_compat",
            reasoning_effort="medium",
            benchmark="bitgn/pac1-dev",
            task_id="t1",
            task_index=0,
            started_at="2026-04-10T00:00:00Z",
            trace_schema_version=TRACE_SCHEMA_VERSION,
        )
    )
    return w


def _mk_adapter_mock(tool_result_content: str = "AGENTS.md contents") -> MagicMock:
    adapter = MagicMock(spec=PcmAdapter)
    adapter.run_prepass = MagicMock()
    adapter.dispatch.return_value = ToolResult(
        ok=True,
        content=tool_result_content,
        refs=("AGENTS.md",),
        error=None,
        error_code=None,
        wall_ms=5,
    )
    adapter.submit_terminal.return_value = ToolResult(
        ok=True, content="", refs=(), error=None, error_code=None, wall_ms=3
    )
    return adapter


def _fake_prepass(session: Session) -> None:
    session.identity_loaded = True
    session.rulebook_loaded = True
    session.seen_refs.add("AGENTS.md")


def _filler_reads(n: int = 3) -> list[NextStepResult]:
    """Return N read-step results — pads scripted backends past R0_MIN_EXPLORE."""
    return [_wrap(_mk_step({"tool": "read", "path": "AGENTS.md"})) for _ in range(n)]


def test_agent_loop_happy_path_read_then_report(tmp_path: Path) -> None:
    backend = _ScriptedBackend(
        _filler_reads(3) + [
            _wrap(_mk_step(
                {
                    "tool": "report_completion",
                    "message": "done",
                    "grounding_refs": ["AGENTS.md"],
                    "rulebook_notes": "n",
                    "outcome_justification": "AGENTS.md was read",
                    "completed_steps_laconic": ["read AGENTS.md"],
                    "outcome": "OUTCOME_OK",
                },
                observation="task complete",
                outcome_leaning="OUTCOME_OK",
            )),
        ]
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
    )
    result: AgentLoopResult = loop.run(task_id="t1", task_text="answer from AGENTS.md")

    assert result.terminated_by == "report_completion"
    assert result.reported == "OUTCOME_OK"
    assert result.enforcer_bypassed is False
    adapter.submit_terminal.assert_called_once()
    writer.close()


def test_agent_loop_enforcer_rejects_fabricated_ref_then_retries(tmp_path: Path) -> None:
    backend = _ScriptedBackend(
        _filler_reads(3) + [
            _wrap(_mk_step(
                {
                    "tool": "report_completion",
                    "message": "done",
                    "grounding_refs": ["imaginary.py"],  # R1 will reject
                    "rulebook_notes": "n",
                    "outcome_justification": "j",
                    "completed_steps_laconic": ["thought about it"],
                    "outcome": "OUTCOME_OK",
                },
                observation="task complete",
                outcome_leaning="OUTCOME_OK",
            )),
            _wrap(_mk_step(
                {
                    "tool": "report_completion",
                    "message": "done",
                    "grounding_refs": ["AGENTS.md"],
                    "rulebook_notes": "n",
                    "outcome_justification": "read AGENTS.md",
                    "completed_steps_laconic": ["read AGENTS.md"],
                    "outcome": "OUTCOME_OK",
                },
                observation="task complete",
                outcome_leaning="OUTCOME_OK",
            )),
        ]
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
    )
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "report_completion"
    assert result.reported == "OUTCOME_OK"
    assert result.enforcer_bypassed is False
    assert backend.calls == 5  # 3 filler reads + one rejected terminal + one accepted retry
    adapter.submit_terminal.assert_called_once()
    writer.close()


def test_agent_loop_submits_anyway_after_exhausted_enforcer_retry(tmp_path: Path) -> None:
    # Both the initial and the retry emit the same bad terminal.
    bad_terminal = _wrap(_mk_step(
        {
            "tool": "report_completion",
            "message": "done",
            "grounding_refs": ["still_fake.py"],
            "rulebook_notes": "n",
            "outcome_justification": "j",
            "completed_steps_laconic": ["-"],
            "outcome": "OUTCOME_OK",
        },
        observation="task complete",
        outcome_leaning="OUTCOME_OK",
    ))
    backend = _ScriptedBackend([bad_terminal, bad_terminal])
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(backend=backend, adapter=adapter, writer=writer, max_steps=5, llm_http_timeout_sec=30.0)
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "report_completion"
    assert result.enforcer_bypassed is True   # submit_anyway path
    adapter.submit_terminal.assert_called_once()
    writer.close()


def test_agent_loop_hits_max_steps_and_fails(tmp_path: Path) -> None:
    # Backend keeps emitting read steps forever — never reaches terminal.
    read_step = _wrap(_mk_step({"tool": "read", "path": "AGENTS.md"}))
    backend = _ScriptedBackend([read_step] * 10)
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(backend=backend, adapter=adapter, writer=writer, max_steps=3, llm_http_timeout_sec=30.0)
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "exhausted"
    assert result.error_kind == "MAX_STEPS"
    writer.close()


class _FlakyBackend(Backend):
    """Raises TransientBackendError once, then returns the canned step."""

    def __init__(self, step: NextStep, raise_times: int = 1) -> None:
        self._step = step
        self._remaining_raises = raise_times
        self.calls = 0

    def next_step(self, messages, response_schema, timeout_sec):  # type: ignore[override]
        self.calls += 1
        if self._remaining_raises > 0:
            self._remaining_raises -= 1
            raise TransientBackendError("429", attempt=self.calls)
        return NextStepResult(
            parsed=self._step,
            prompt_tokens=0,
            completion_tokens=0,
            reasoning_tokens=0,
        )


def test_agent_loop_retries_on_transient_backend_error(tmp_path: Path, monkeypatch) -> None:
    # Replace time.sleep so tests stay fast.
    monkeypatch.setattr("bitgn_contest_agent.agent.time.sleep", lambda s: None)
    # Use OUTCOME_DENIED_SECURITY to bypass R0_MIN_EXPLORE — this test
    # is about transient-error retry, not terminal validation.
    backend = _FlakyBackend(
        _mk_step(
            {
                "tool": "report_completion",
                "message": "denied",
                "grounding_refs": [],
                "rulebook_notes": "n",
                "outcome_justification": "security",
                "completed_steps_laconic": ["checked"],
                "outcome": "OUTCOME_DENIED_SECURITY",
            },
            observation="security concern",
            outcome_leaning="OUTCOME_DENIED_SECURITY",
        ),
        raise_times=2,
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=5,
        llm_http_timeout_sec=30.0,
        backend_backoff_ms=(1, 1, 1, 1),
    )
    result = loop.run(task_id="t1", task_text="do it")

    assert result.terminated_by == "report_completion"
    assert backend.calls == 3  # 2 transient failures + 1 success
    writer.close()


def test_agent_loop_sleeps_for_reload_after_model_unloaded(tmp_path: Path, monkeypatch) -> None:
    """After the watchdog force-unloads LM Studio, the reloaded slot needs
    ~9-12s before a new request can land. The generic backoff schedule
    ((500, 1500, 4000, 10000) ms) would otherwise spend its first three
    retries inside that reload window. On a 'Model unloaded.' transient,
    the agent loop must add a ~12s reload wait on top of the normal
    backoff so the retry hits a ready slot."""
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "bitgn_contest_agent.agent.time.sleep",
        lambda s: sleep_calls.append(s),
    )
    backend = _FlakyBackend(
        _mk_step(
            {
                "tool": "report_completion",
                "message": "denied",
                "grounding_refs": [],
                "rulebook_notes": "n",
                "outcome_justification": "security",
                "completed_steps_laconic": ["checked"],
                "outcome": "OUTCOME_DENIED_SECURITY",
            },
            observation="security concern",
            outcome_leaning="OUTCOME_DENIED_SECURITY",
        ),
        raise_times=1,
    )
    # Monkey-patch the next_step to raise a Model-unloaded-flavored error.
    original_next = backend.next_step
    def _raising_next_step(messages, response_schema, timeout_sec):  # type: ignore[override]
        if backend._remaining_raises > 0:
            backend.calls += 1
            backend._remaining_raises -= 1
            raise TransientBackendError(
                "Error code: 400 - {'error': 'Model unloaded.'}"
            )
        return original_next(messages, response_schema, timeout_sec)
    backend.next_step = _raising_next_step  # type: ignore[assignment]

    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=5,
        llm_http_timeout_sec=30.0,
        backend_backoff_ms=(1, 1, 1, 1),
    )
    result = loop.run(task_id="t1", task_text="do it")
    assert result.terminated_by == "report_completion"
    # The 12.0s post-unload reload wait must have been issued exactly once.
    assert 12.0 in sleep_calls, (
        f"expected a 12.0s reload sleep after Model unloaded; got {sleep_calls!r}"
    )
    writer.close()


def test_agent_loop_fails_task_after_backend_exhaustion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bitgn_contest_agent.agent.time.sleep", lambda s: None)

    class _AlwaysFlaky(Backend):
        def next_step(self, messages, response_schema, timeout_sec):  # type: ignore[override]
            raise TransientBackendError("no capacity")

    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=_AlwaysFlaky(),
        adapter=adapter,
        writer=writer,
        max_steps=5,
        llm_http_timeout_sec=30.0,
        backend_backoff_ms=(1, 1, 1, 1),
    )
    result = loop.run(task_id="t1", task_text="do it")
    assert result.terminated_by == "error"
    assert result.error_kind == "BACKEND_ERROR"
    writer.close()


def test_agent_loop_writes_real_tokens_into_trace_and_totals(tmp_path: Path, monkeypatch) -> None:
    """Tokens from NextStepResult must end up in both the step record and
    the outcome's total fields. Verifies T1.6 plumbing end-to-end."""
    report_step = _mk_step(
        {
            "tool": "report_completion",
            "message": "done",
            "grounding_refs": ["AGENTS.md"],
            "rulebook_notes": "n",
            "outcome_justification": "AGENTS.md was read",
            "completed_steps_laconic": ["read AGENTS.md"],
            "outcome": "OUTCOME_OK",
        },
        observation="task complete",
        outcome_leaning="OUTCOME_OK",
    )
    backend = _ScriptedBackend(
        _filler_reads(3) + [
            NextStepResult(parsed=report_step, prompt_tokens=137, completion_tokens=42, reasoning_tokens=9),
        ]
    )
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
    )
    result = loop.run(task_id="t1", task_text="x")
    writer.close()

    # Outcome totals:
    assert result.total_prompt_tokens == 137
    assert result.total_completion_tokens == 42
    assert result.total_reasoning_tokens == 9

    # Step record carries them too:
    trace_path = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    step_records = [r for r in records if r.get("kind") == "step"]
    assert step_records, "no step record written"
    # The report_completion step is the last one (filler reads precede it).
    llm = step_records[-1]["llm"]
    assert llm["prompt_tokens"] == 137
    assert llm["completion_tokens"] == 42
    assert llm["reasoning_tokens"] == 9


def test_agent_loop_dispatches_parallel_reads(tmp_path: Path) -> None:
    """parallel_reads → all entries dispatch concurrently with the primary
    function; the user message back to the planner stitches every result."""
    step1 = NextStep(
        current_state="batch read",
        plan_remaining_steps_brief=["batch", "report"],
        identity_verified=True,
        observation="read 3 entity files in parallel",
        outcome_leaning="GATHERING_INFORMATION",
        function={"tool": "read", "path": "10_entities/cast/foo.md"},
        parallel_reads=[
            {"tool": "read", "path": "10_entities/cast/bar.md"},
            {"tool": "list", "name": "60_outbox"},
        ],
    )
    terminal_step = _mk_step(
        {
            "tool": "report_completion",
            "message": "ok",
            "grounding_refs": ["10_entities/cast/foo.md", "10_entities/cast/bar.md"],
            "rulebook_notes": "n",
            "outcome_justification": "read three sources",
            "completed_steps_laconic": ["batched 3 reads"],
            "outcome": "OUTCOME_OK",
        },
        observation="task complete",
        outcome_leaning="OUTCOME_OK",
    )
    # Provide an extra copy in case the verify-hook fires.
    backend = _ScriptedBackend([_wrap(step1), _wrap(terminal_step), _wrap(terminal_step)])

    adapter = MagicMock(spec=PcmAdapter)
    adapter.run_prepass = MagicMock(
        side_effect=lambda *, session, trace_writer: _fake_prepass(session)
    )
    # Distinct ToolResult per dispatch so we can verify ordering preserved.
    def _dispatch(req):
        path = getattr(req, "path", None) or getattr(req, "name", None) or "?"
        return ToolResult(
            ok=True, content=f"BODY[{path}]",
            refs=(path,) if path != "?" else (),
            error=None, error_code=None, wall_ms=1,
        )
    adapter.dispatch.side_effect = _dispatch
    adapter.submit_terminal.return_value = ToolResult(
        ok=True, content="", refs=(), error=None, error_code=None, wall_ms=1
    )
    writer = _mk_writer(tmp_path)

    loop = AgentLoop(
        backend=backend, adapter=adapter, writer=writer,
        max_steps=5, llm_http_timeout_sec=30.0,
    )
    result = loop.run(task_id="t-parallel", task_text="read three things")
    writer.close()

    assert result.terminated_by == "report_completion"
    # 3 dispatch() calls (one per op in step 1) + 0 (terminal uses submit_terminal).
    assert adapter.dispatch.call_count == 3
    # The trace event records the parallel dispatch.
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    events = [r for r in records if r.get("kind") == "event"]
    kinds = [e.get("event_kind") for e in events]
    assert "parallel_reads_dispatched" in kinds


def test_agent_loop_drops_parallel_reads_when_primary_is_write(tmp_path: Path) -> None:
    """parallel_reads supplied next to a write → silently dropped; the
    write still runs solo and a trace event is emitted."""
    step1 = NextStep(
        current_state="bad batch",
        plan_remaining_steps_brief=["write", "report"],
        identity_verified=True,
        observation="attempt batch alongside write",
        outcome_leaning="GATHERING_INFORMATION",
        function={"tool": "write", "path": "out.txt", "content": "hello"},
        parallel_reads=[{"tool": "read", "path": "AGENTS.md"}],
    )
    terminal_step = _mk_step(
        {
            "tool": "report_completion",
            "message": "ok",
            "grounding_refs": ["AGENTS.md"],
            "rulebook_notes": "n",
            "outcome_justification": "wrote file",
            "completed_steps_laconic": ["wrote out.txt"],
            "outcome": "OUTCOME_OK",
        },
        observation="task complete",
        outcome_leaning="OUTCOME_OK",
    )
    backend = _ScriptedBackend([_wrap(step1), _wrap(terminal_step), _wrap(terminal_step)])
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path)
    loop = AgentLoop(
        backend=backend, adapter=adapter, writer=writer,
        max_steps=5, llm_http_timeout_sec=30.0,
    )
    loop.run(task_id="t-bad-batch", task_text="x")
    writer.close()

    # Only one dispatch — the write. parallel_reads was dropped.
    assert adapter.dispatch.call_count == 1
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    events = [r for r in records if r.get("kind") == "event"]
    kinds = [e.get("event_kind") for e in events]
    assert "parallel_reads_dropped" in kinds
