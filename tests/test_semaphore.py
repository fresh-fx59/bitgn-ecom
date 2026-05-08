"""T2.1: threading.Semaphore bounds backend.next_step concurrency."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Sequence

from bitgn_contest_agent.agent import AgentLoop
from bitgn_contest_agent.backend.base import Backend, Message, NextStepResult
from bitgn_contest_agent.schemas import NextStep

# Reuse test helpers from test_agent_loop
from tests.test_agent_loop import _mk_step, _mk_adapter_mock, _mk_writer, _fake_prepass


class _SlowBackend(Backend):
    """Blocks inside next_step so we can count concurrent callers."""

    def __init__(self) -> None:
        self.concurrent = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.calls = 0

    def next_step(
        self,
        messages: Sequence[Message],
        response_schema,
        timeout_sec,
    ):  # type: ignore[override]
        with self.lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
            self.calls += 1
        try:
            time.sleep(0.05)  # 50ms — long enough for all threads to overlap
        finally:
            with self.lock:
                self.concurrent -= 1
        # Return a terminal step so the agent loop exits after one call.
        # Use DENIED_SECURITY to bypass R0_MIN_EXPLORE — this test is
        # about concurrency, not terminal validation.
        return NextStepResult(
            parsed=_mk_step(
                {
                    "tool": "report_completion",
                    "message": "denied",
                    "grounding_refs": [],
                    "rulebook_notes": "n",
                    "outcome_justification": "security",
                    "completed_steps_laconic": ["checked"],
                    "outcome": "OUTCOME_DENIED_SECURITY",
                },
                outcome_leaning="OUTCOME_DENIED_SECURITY",
            ),
            prompt_tokens=0,
            completion_tokens=0,
            reasoning_tokens=0,
        )


def _run_one_agent(backend: _SlowBackend, semaphore: threading.Semaphore, tmp_path: Path, idx: int) -> None:
    """Spin up a minimal AgentLoop and run one task. Designed to block inside
    backend.next_step under the semaphore."""
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    # _mk_writer appends "trace.jsonl" internally, so pass a unique subdir
    writer = _mk_writer(tmp_path / str(idx))
    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
        inflight_semaphore=semaphore,
    )
    try:
        loop.run(task_id=f"t{idx}", task_text="x")
    finally:
        writer.close()


def test_semaphore_bounds_concurrent_next_step_calls(tmp_path: Path) -> None:
    """With semaphore=3 and 10 parallel agents, peak concurrency at
    backend.next_step must be <= 3."""
    backend = _SlowBackend()
    semaphore = threading.Semaphore(3)
    threads = [
        threading.Thread(target=_run_one_agent, args=(backend, semaphore, tmp_path, i))
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert backend.calls == 10, f"expected 10 backend calls, got {backend.calls}"
    assert backend.peak <= 3, f"peak concurrency {backend.peak} exceeded limit 3"


def test_agent_loop_accepts_none_semaphore(tmp_path: Path) -> None:
    """AgentLoop must work without a semaphore (existing call sites pass None)."""
    # If we got here without raising, the default=None path is wired.
    # Full behavioral coverage lives in test_agent_loop.py.
    backend = _SlowBackend()
    adapter = _mk_adapter_mock()
    adapter.run_prepass.side_effect = lambda *, session, trace_writer: _fake_prepass(session)
    writer = _mk_writer(tmp_path / "none_sem")
    loop = AgentLoop(
        backend=backend,
        adapter=adapter,
        writer=writer,
        max_steps=10,
        llm_http_timeout_sec=30.0,
        # inflight_semaphore not passed — should default to None
    )
    loop.run(task_id="t0", task_text="x")
    writer.close()
    assert backend.calls == 1
