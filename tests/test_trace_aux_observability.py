"""Additive trace observability for the auxiliary-LLM layer + enforcers.

These traces fill a gap discovered after a 100-task PROD run where the
aux model (claude-haiku-4-5) was 400-failing on EVERY call and the
existing trace could not tell us (a) which aux model was used, (b) the
error type / body, (c) which enforcer changed grounding_refs, or (d) the
per-run failure breakdown. All additions are observational only and must
not alter agent decisions.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai as _openai

from bitgn_contest_agent import classifier as classifier_module
from bitgn_contest_agent.trace_schema import (
    TRACE_SCHEMA_VERSION,
    TraceAuxCall,
    TraceEnforcerMod,
    TraceOutcome,
    load_jsonl,
)
from bitgn_contest_agent.trace_writer import TraceWriter


# ── 1. Per-call aux LLM trace event ─────────────────────────────────────


def test_append_aux_call_success(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_aux_call(
        model="claude-haiku-4-5-20251001",
        purpose="classify",
        attempts=1,
        ok=True,
    )
    w.close()
    records = list(load_jsonl(path))
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, TraceAuxCall)
    assert rec.kind == "aux_call"
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.purpose == "classify"
    assert rec.attempts == 1
    assert rec.ok is True
    assert rec.error_type is None
    assert rec.error_msg is None


def test_append_aux_call_failure_records_error_type_and_truncated_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    long_body = "Unrecognized request arguments: reasoning. " + ("x" * 5000)
    w.append_aux_call(
        model="some-cheap-model",
        purpose="ref_judge",
        attempts=3,
        ok=False,
        error_type="BadRequestError",
        error_msg=long_body,
    )
    w.close()
    rec = list(load_jsonl(path))[0]
    assert isinstance(rec, TraceAuxCall)
    assert rec.ok is False
    assert rec.attempts == 3
    assert rec.error_type == "BadRequestError"
    # Body is truncated to keep traces small but must keep the signal.
    assert rec.error_msg is not None
    assert "Unrecognized request arguments: reasoning" in rec.error_msg
    assert len(rec.error_msg) <= 600


# ── 2. classifier._llm_call emits an aux_call event per call ─────────────


def _install_writer(tmp_path: Path, monkeypatch) -> TraceWriter:
    """Install a task-scoped TraceWriter so emit helpers can find it."""
    from bitgn_contest_agent import arch_log

    w = TraceWriter(path=tmp_path / "trace.jsonl")
    token = arch_log.set_task_context(
        task_id="t1", run_index=0, trace_name="trace", writer=w,
    )
    monkeypatch.setattr(
        arch_log, "reset_task_context", arch_log.reset_task_context,
    )
    # ensure context is reset even if the test raises
    import atexit

    atexit.register(lambda: arch_log.reset_task_context(token))
    return w


def test_llm_call_success_emits_aux_call_event(tmp_path: Path, monkeypatch) -> None:
    from bitgn_contest_agent import arch_log

    classifier_module.reset_aux_coverage()
    w = TraceWriter(path=tmp_path / "trace.jsonl")
    token = arch_log.set_task_context(
        task_id="t1", run_index=0, trace_name="trace", writer=w,
    )
    try:
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        )
        classifier_module._llm_call(
            client, model="claude-haiku-4-5-20251001", messages=[], stream=False,
        )
    finally:
        arch_log.reset_task_context(token)
    w.close()

    aux = [r for r in load_jsonl(w.path) if isinstance(r, TraceAuxCall)]
    assert len(aux) == 1
    assert aux[0].ok is True
    assert aux[0].model == "claude-haiku-4-5-20251001"
    assert aux[0].attempts >= 1


def test_llm_call_400_failure_emits_aux_call_event_with_error_type(
    tmp_path: Path, monkeypatch
) -> None:
    """The blackout case: a BadRequestError 400 must produce a failure
    event naming the error type and carrying the truncated body."""
    from bitgn_contest_agent import arch_log

    classifier_module.reset_aux_coverage()
    w = TraceWriter(path=tmp_path / "trace.jsonl")
    token = arch_log.set_task_context(
        task_id="t1", run_index=0, trace_name="trace", writer=w,
    )

    # Build a real BadRequestError shape with a body the agent would see.
    err = _openai.BadRequestError(
        message="Unrecognized request arguments: reasoning",
        response=MagicMock(status_code=400),
        body=None,
    )

    try:
        client = MagicMock()
        client.chat.completions.create.side_effect = err
        raised = False
        try:
            classifier_module._llm_call(
                client,
                model="claude-haiku-4-5-20251001",
                messages=[],
                stream=False,
            )
        except _openai.BadRequestError:
            raised = True
        assert raised, "the 400 must still propagate — logging is additive"
    finally:
        arch_log.reset_task_context(token)
    w.close()

    aux = [r for r in load_jsonl(w.path) if isinstance(r, TraceAuxCall)]
    assert len(aux) == 1
    ev = aux[0]
    assert ev.ok is False
    assert ev.error_type == "BadRequestError"
    assert ev.error_msg is not None
    assert "Unrecognized request arguments: reasoning" in ev.error_msg


def test_llm_call_failure_increments_failure_breakdown(monkeypatch) -> None:
    """The per-thread aux coverage must track failures by error type so a
    run-level blackout is summarizable."""
    classifier_module.reset_aux_coverage()
    err = _openai.BadRequestError(
        message="bad response status code 400",
        response=MagicMock(status_code=400),
        body=None,
    )
    client = MagicMock()
    client.chat.completions.create.side_effect = err
    for _ in range(2):
        try:
            classifier_module._llm_call(
                client, model="m", messages=[], stream=False,
            )
        except _openai.BadRequestError:
            pass

    breakdown = classifier_module.get_aux_failures_by_type()
    assert breakdown.get("BadRequestError") == 2
    attempted, succeeded = classifier_module.get_aux_coverage()
    assert attempted == 2
    assert succeeded == 0


# ── 3. Enforcer/completer modification trace event ──────────────────────


def test_append_enforcer_mod_records_before_after_and_diff(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_enforcer_mod(
        enforcer="cite_completer",
        refs_before=["/a.json"],
        refs_after=["/a.json", "/b.json"],
        bypassed=False,
    )
    w.close()
    rec = list(load_jsonl(path))[0]
    assert isinstance(rec, TraceEnforcerMod)
    assert rec.kind == "enforcer_mod"
    assert rec.enforcer == "cite_completer"
    assert rec.refs_before == ["/a.json"]
    assert rec.refs_after == ["/a.json", "/b.json"]
    assert rec.refs_added == ["/b.json"]
    assert rec.refs_removed == []
    assert rec.bypassed is False


def test_append_enforcer_mod_records_removed_refs(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_enforcer_mod(
        enforcer="catalog_strip_enforcer",
        refs_before=["/a.json", "/b.json"],
        refs_after=["/a.json"],
    )
    w.close()
    rec = list(load_jsonl(path))[0]
    assert isinstance(rec, TraceEnforcerMod)
    assert rec.refs_added == []
    assert rec.refs_removed == ["/b.json"]


# ── 4. Outcome carries the run-level aux blackout summary ────────────────


def test_outcome_accepts_aux_coverage_and_failure_breakdown(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_outcome(
        TraceOutcome(
            terminated_by="report_completion",
            reported="OUTCOME_OK",
            total_steps=1,
            total_llm_calls=1,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            aux_attempted=50,
            aux_succeeded=0,
            aux_failures_by_type={"BadRequestError": 50},
        )
    )
    w.close()
    rec = list(load_jsonl(path))[-1]
    assert isinstance(rec, TraceOutcome)
    assert rec.aux_attempted == 50
    assert rec.aux_succeeded == 0
    assert rec.aux_failures_by_type == {"BadRequestError": 50}


def test_outcome_aux_fields_default_absent_for_backcompat(tmp_path: Path) -> None:
    """Old summaries that don't know these fields must keep parsing; the
    fields are absent/None when not populated."""
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_outcome(
        TraceOutcome(
            terminated_by="report_completion",
            reported="OUTCOME_OK",
            total_steps=1,
            total_llm_calls=1,
            total_prompt_tokens=0,
            total_completion_tokens=0,
        )
    )
    w.close()
    rec = list(load_jsonl(path))[-1]
    assert isinstance(rec, TraceOutcome)
    assert rec.aux_attempted is None
    assert rec.aux_succeeded is None
    assert rec.aux_failures_by_type is None
    # Dumped JSON line must omit/null the new fields (no crash for old readers)
    raw = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert raw.get("aux_attempted") is None


# ── 5. New kinds must be registered so load_jsonl does not raise ─────────


def test_load_jsonl_accepts_new_kinds(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_aux_call(model="m", purpose="normalise", attempts=1, ok=True)
    w.append_enforcer_mod(
        enforcer="e", refs_before=[], refs_after=["/x"],
    )
    w.close()
    kinds = [r.kind for r in load_jsonl(path)]
    assert kinds == ["aux_call", "enforcer_mod"]
