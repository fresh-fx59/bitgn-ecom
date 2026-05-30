"""End-to-end loop test for the count_per_store re-derivation BOUNCE.

Proves the full wiring (no LLM, no PROD): a scripted backend proposes a
count terminal, the agent loop runs the deterministic SQL re-derivation
against a real /bin/sql adapter backed by the t45_real2 oracle DB, and on
disagreement the terminal is BOUNCED (verdict flipped → existing retry
path re-prompts) so the corrected answer is the one submitted. The gate is
default-off, so an un-flagged run submits the agent's (wrong) answer as-is.

Companion to tests/test_count_rederive.py (oracle unit tests) and
tests/test_count_rederive_integration.py (reason-builder). See
docs/SPEC_RELIABILITY_53.md.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.adapter.ecom import ToolResult
from bitgn_contest_agent.agent import AgentLoop
from bitgn_contest_agent.backend.base import Backend, Message, NextStepResult
from bitgn_contest_agent.schemas import NextStep
from bitgn_contest_agent.validator import Verdict

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "artifacts" / "ws_snapshots" / "t45_real2"

# The t45_real2 instruction ("fewer than 4" at Wilten/Innsbruck) and the
# six products, parsed exactly as the LLM's task_spec would carry them.
# Controller-verified oracle: the re-derivation yields 4.
_T45_INSTRUCTION = json.loads((SNAP / "run_0" / "metadata.json").read_text())["instruction"]
_T45_PRODUCTS = [
    {"brand": "Fiskars", "model": "1CD-A3X", "attributes": {"power_source": "battery"}},
    {"brand": "Mobil", "model": "1ZE-TCR", "attributes": {"volume": "5000 ml", "viscosity": "15W-40"}},
    {"brand": "Keter", "model": "2OO-VJU", "attributes": {"storage_type": "parts case", "color_family": "Yellow", "volume": "8 l"}},
    {"brand": "Engelbert Strauss", "model": "37H-N9K", "attributes": {"color_family": "Black"}},
    {"brand": "Sika", "model": "28T-UV8", "attributes": {"sealant_type": "hybrid sealant", "color_family": "Gray", "volume": "300 ml"}},
    {"brand": "Sonax", "model": "304-ZK0", "attributes": {"length": "450 mm"}},
]
_T45_SPEC = {
    "kind": "count_per_store",
    "store_descriptor": "Wilten PowerTool store in Innsbruck",
    "threshold": 4,
    "products": _T45_PRODUCTS,
}


def _build_db() -> sqlite3.Connection:
    schema = (SNAP / "sql_schema.sql").read_text()
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    for jf in (SNAP / "sql").glob("*.json"):
        rows = json.loads(jf.read_text())
        if not rows:
            continue
        cols = list(rows[0].keys())
        conn.executemany(
            f'INSERT INTO "{jf.stem}" ({",".join(chr(34) + c + chr(34) for c in cols)}) '
            f'VALUES ({",".join("?" for _ in cols)})',
            [tuple(r.get(c) for c in cols) for r in rows],
        )
    conn.commit()
    return conn


class _SqlAdapter:
    """Serves /bin/sql from the in-memory oracle DB (CSV, like PROD)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.submitted = None

    def run_prepass(self, *, session, trace_writer, task_text=""):
        session.identity_loaded = True
        session.rulebook_loaded = True
        session.seen_refs.add("AGENTS.md")
        return None

    def _run_sql_csv(self, sql: str):
        try:
            cur = self._conn.execute(sql)
        except sqlite3.Error:
            return None
        rows = cur.fetchall()
        header = ",".join(d[0] for d in cur.description) if cur.description else ""
        return "\n".join(
            [header] + [",".join("" if c is None else str(c) for c in r) for r in rows]
        )

    def dispatch(self, fn) -> ToolResult:
        if getattr(fn, "tool", "") == "exec" and getattr(fn, "path", "") == "/bin/sql":
            csv = self._run_sql_csv(getattr(fn, "stdin", "") or "")
            return ToolResult(
                ok=csv is not None, content=csv or "", refs=(),
                error=None, error_code=None, wall_ms=1,
            )
        return ToolResult(ok=True, content="", refs=(), error=None, error_code=None, wall_ms=1)

    def submit_terminal(self, fn) -> ToolResult:
        self.submitted = fn
        return ToolResult(ok=True, content="accepted", refs=(), error=None, error_code=None, wall_ms=1)


class _OkValidator:
    """Accepts every terminal — isolates the bounce as the unit under test."""

    def check_step(self, step_obj, session, step_idx, max_steps, **kw):
        return None

    def check_terminal(self, session, step_obj, step_idx=99):
        return Verdict(ok=True, reasons=[])


class _ScriptedBackend(Backend):
    def __init__(self, scripted: list[NextStepResult]) -> None:
        self._steps = list(scripted)
        self.calls = 0

    def next_step(self, messages: Sequence[Message], response_schema, timeout_sec):  # type: ignore[override]
        self.calls += 1
        return self._steps.pop(0)


def _count_terminal(count_token: str) -> NextStepResult:
    step = NextStep(
        current_state="x",
        plan_remaining_steps_brief=["report"],
        identity_verified=True,
        observation="counted",
        outcome_leaning="OUTCOME_OK",
        function={
            "tool": "report_completion",
            "message": count_token,
            "grounding_refs": [],
            "rulebook_notes": "n",
            "outcome_justification": "counted availability per store",
            "completed_steps_laconic": ["queried inventory"],
            "outcome": "OUTCOME_OK",
            "task_spec": _T45_SPEC,
        },
    )
    return NextStepResult(parsed=step, prompt_tokens=0, completion_tokens=0, reasoning_tokens=0)


def _build_loop(backend) -> tuple[AgentLoop, _SqlAdapter]:
    adapter = _SqlAdapter(_build_db())
    loop = AgentLoop(
        backend=backend, adapter=adapter, writer=MagicMock(),
        max_steps=10, llm_http_timeout_sec=30.0,
    )
    loop._validator = _OkValidator()
    return loop, adapter


@pytest.fixture(autouse=True)
def _no_verify(monkeypatch):
    # Keep backend.calls deterministic: skip the pre-completion verify round.
    monkeypatch.setattr("bitgn_contest_agent.verify.should_verify", lambda **kw: [])


def test_bounce_corrects_wrong_count_when_enabled(monkeypatch):
    monkeypatch.setenv("BITGN_USE_REDERIVE_COUNT", "1")
    backend = _ScriptedBackend([_count_terminal("<COUNT:3>"), _count_terminal("<COUNT:4>")])
    loop, adapter = _build_loop(backend)

    loop.run(task_id="t45", task_text=_T45_INSTRUCTION)

    # The wrong <COUNT:3> was bounced; the loop re-prompted and submitted the
    # corrected <COUNT:4> (the oracle). Two backend calls = terminal + retry.
    assert backend.calls == 2
    assert adapter.submitted is not None
    assert adapter.submitted.message == "<COUNT:4>"


def test_no_bounce_when_count_already_correct(monkeypatch):
    monkeypatch.setenv("BITGN_USE_REDERIVE_COUNT", "1")
    backend = _ScriptedBackend([_count_terminal("<COUNT:4>")])
    loop, adapter = _build_loop(backend)

    loop.run(task_id="t45", task_text=_T45_INSTRUCTION)

    # Agreement → no bounce, no retry. A single backend call; the agent's
    # own (correct) answer is submitted unchanged.
    assert backend.calls == 1
    assert adapter.submitted.message == "<COUNT:4>"


def test_gate_off_by_default_submits_agent_answer(monkeypatch):
    monkeypatch.delenv("BITGN_USE_REDERIVE_COUNT", raising=False)
    backend = _ScriptedBackend([_count_terminal("<COUNT:3>")])
    loop, adapter = _build_loop(backend)

    loop.run(task_id="t45", task_text=_T45_INSTRUCTION)

    # Flag off → the re-derivation never runs; the wrong answer stands.
    assert backend.calls == 1
    assert adapter.submitted.message == "<COUNT:3>"
