"""Minimal mock-backend harness for verify integration tests.

Runs the real `AgentLoop.run(...)` loop with a pytest-friendly stub for the
backend, adapter, router, validator, and trace writer. Returns the list
of trace records the run would have written.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence
from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.agent import AgentLoop
import bitgn_contest_agent.agent as _agent_mod
from bitgn_contest_agent.backend.base import Message, NextStepResult
from bitgn_contest_agent.router import RoutingDecision
from bitgn_contest_agent.schemas import NextStep
from bitgn_contest_agent.validator import Verdict


class _Writer:
    """Spy writer that captures verify events while silently absorbing others."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append_task(self, **kw): pass
    def append_step(self, **kw): pass
    def append_event(self, **kw): pass
    def append_pcm_op(self, **kw): pass
    def append_prepass(self, **kw): pass
    def append_verify(self, **kw): self.records.append({"kind": "verify", **kw})
    def append_outcome(self, outcome): pass
    def append_arch(self, record): pass
    def close(self): pass


class _Adapter:
    def __init__(self) -> None:
        self.reads: dict[str, str] = {
            "50_finance/purchases/bill_a.md": '{"content": "amount: 6"}',
            "50_finance/purchases/bill_b.md": '{"content": "amount: 6"}',
        }

    def run_prepass(self, *, session, trace_writer):
        @dataclass
        class _Prepass:
            bootstrap_content: list[str] = ()
            schema: Any = None
        return _Prepass(bootstrap_content=[])

    def dispatch(self, fn) -> ToolResult:
        path = getattr(fn, "path", "")
        if getattr(fn, "tool", "") == "read" and path in self.reads:
            return ToolResult(
                ok=True, content=self.reads[path], refs=(),
                error=None, error_code=None, wall_ms=1,
            )
        return ToolResult(
            ok=True, content="", refs=(),
            error=None, error_code=None, wall_ms=1,
        )

    def submit_terminal(self, fn) -> ToolResult:
        return ToolResult(
            ok=True, content="accepted", refs=(),
            error=None, error_code=None, wall_ms=1,
        )


class _Validator:
    def check_step(self, step_obj, session, step_idx, max_steps, **kw):
        return None  # no correction

    def check_terminal(self, session, step_obj, step_idx=99):
        return Verdict(ok=True, reasons=[])


class _Backend:
    def __init__(self, handler: Callable[..., dict]) -> None:
        self.handler = handler

    def next_step(
        self,
        messages: Sequence[Message],
        response_schema: type,
        timeout_sec: float,
    ) -> NextStepResult:
        raw = self.handler(messages)
        parsed = NextStep.model_validate(raw)
        return NextStepResult(
            parsed=parsed,
            prompt_tokens=10, completion_tokens=5, reasoning_tokens=0,
        )


class _HarnessAgentLoop(AgentLoop):
    """AgentLoop subclass that optionally forces a skill_name in the routing decision."""

    def __init__(self, *, forced_skill_name: str | None, **kw):
        super().__init__(**kw)
        self._forced_skill_name = forced_skill_name

    def run(self, *, task_id: str, task_text: str):
        forced = self._forced_skill_name
        if forced is None:
            return super().run(task_id=task_id, task_text=task_text)

        real_build = _agent_mod._build_initial_messages

        def fake_build(*args, **kwargs):
            messages, decision = real_build(*args, **kwargs)
            decision = RoutingDecision(
                category=decision.category if decision else "INBOX_PROCESSING",
                source=decision.source if decision else "regex",
                confidence=decision.confidence if decision else 1.0,
                extracted=decision.extracted if decision else {},
                skill_name=forced,
                task_text=task_text,
            )
            return messages, decision

        _agent_mod._build_initial_messages = fake_build
        try:
            return super().run(task_id=task_id, task_text=task_text)
        finally:
            _agent_mod._build_initial_messages = real_build


def run_agent_with_mock_backend(
    *, task_id: str, task_text: str,
    backend: Callable[..., dict],
    skill_name: str | None = None,
) -> list[dict]:
    writer = _Writer()
    agent = _HarnessAgentLoop(
        forced_skill_name=skill_name,
        backend=_Backend(backend),
        adapter=_Adapter(),
        writer=writer,
        max_steps=5,
        llm_http_timeout_sec=30.0,
    )
    # The AgentLoop has its own internal StepValidator. We monkeypatch it
    # after construction so the harness can bypass real terminal validation:
    agent._validator = _Validator()
    agent.run(task_id=task_id, task_text=task_text)
    return writer.records
