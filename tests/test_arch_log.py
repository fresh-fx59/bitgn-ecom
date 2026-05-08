# tests/test_arch_log.py
"""Central arch logging: emit, formatter, ContextVar filter."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ArchResult,
    RouterSource,
    ValidatorT1Rule,
    ValidatorT2Trigger,
)


def test_format_arch_line_minimal() -> None:
    from bitgn_contest_agent.arch_log import _format_arch_line
    from bitgn_contest_agent.trace_schema import TraceArch
    rec = TraceArch(category=ArchCategory.SKILL_ROUTER)
    line = _format_arch_line(rec)
    assert line.startswith("[ARCH:SKILL_ROUTER]")


def test_format_arch_line_full() -> None:
    from bitgn_contest_agent.arch_log import _format_arch_line
    from bitgn_contest_agent.trace_schema import TraceArch
    rec = TraceArch(
        category=ArchCategory.VALIDATOR_T1,
        at_step=3,
        rule=ValidatorT1Rule.MUTATION_GUARD,
        details="tool=write",
    )
    line = _format_arch_line(rec)
    assert "[ARCH:VALIDATOR_T1]" in line
    assert "step=3" in line
    assert "rule=mutation_guard" in line
    assert "details=tool=write" in line


def test_format_arch_line_skips_none_fields() -> None:
    from bitgn_contest_agent.arch_log import _format_arch_line
    from bitgn_contest_agent.trace_schema import TraceArch
    rec = TraceArch(category=ArchCategory.TERMINAL, result=ArchResult.ACCEPT)
    line = _format_arch_line(rec)
    assert "result=ACCEPT" in line
    assert "rule=" not in line
    assert "trigger=" not in line


def test_emit_arch_writes_both_writer_and_stderr(tmp_path, caplog) -> None:
    from bitgn_contest_agent.arch_log import emit_arch
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    with caplog.at_level(logging.INFO, logger="bitgn_contest_agent.arch_log"):
        emit_arch(
            writer,
            category=ArchCategory.VALIDATOR_T2,
            at_step=5,
            trigger=ValidatorT2Trigger.FIRST_TRANSITION,
            result=ArchResult.OK,
        )
    writer.close()
    records = list(load_jsonl(p))
    assert len(records) == 1
    assert isinstance(records[0], TraceArch)
    assert records[0].trigger == ValidatorT2Trigger.FIRST_TRANSITION
    assert any("[ARCH:VALIDATOR_T2]" in r.message for r in caplog.records)


def test_emit_arch_writer_may_be_none(caplog) -> None:
    # Tests / no-writer paths still log to stderr.
    from bitgn_contest_agent.arch_log import emit_arch
    with caplog.at_level(logging.INFO, logger="bitgn_contest_agent.arch_log"):
        emit_arch(None, category=ArchCategory.LOOP_NUDGE, at_step=1, details="x")
    assert any("[ARCH:LOOP_NUDGE]" in r.message for r in caplog.records)


def test_emit_arch_records_emitted_at(tmp_path) -> None:
    from bitgn_contest_agent.arch_log import emit_arch
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    emit_arch(writer, category=ArchCategory.TASK_START, details="x")
    writer.close()
    rec = next(iter(load_jsonl(p)))
    assert isinstance(rec, TraceArch)
    assert rec.emitted_at is not None
    assert rec.emitted_at.endswith("+00:00")  # UTC ISO-8601


def test_task_context_filter_injects_defaults_when_unset() -> None:
    from bitgn_contest_agent.arch_log import TaskContextFilter
    f = TaskContextFilter()
    rec = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    assert f.filter(rec) is True
    assert rec.task_id == "-"
    assert rec.run_index == "-"
    assert rec.trace_name == "-"
    assert rec.skill == "-"
    assert rec.category == "-"


def test_task_context_set_and_reset() -> None:
    from bitgn_contest_agent.arch_log import (
        TaskContextFilter, set_task_context, reset_task_context,
    )
    f = TaskContextFilter()
    token = set_task_context(
        task_id="t100", run_index=0,
        trace_name="t100__run0.jsonl",
    )
    try:
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        assert rec.task_id == "t100"
        assert rec.run_index == 0
        assert rec.trace_name == "t100__run0.jsonl"
        assert rec.skill == "-"
        assert rec.category == "-"
    finally:
        reset_task_context(token)
    # After reset, defaults are back.
    rec2 = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="m", args=(), exc_info=None,
    )
    f.filter(rec2)
    assert rec2.task_id == "-"


def test_emit_arch_uses_writer_from_context(tmp_path) -> None:
    from bitgn_contest_agent.arch_log import (
        emit_arch, set_task_context, reset_task_context,
    )
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t1", run_index=0, trace_name="t.jsonl", writer=writer,
    )
    try:
        emit_arch(category=ArchCategory.TASK_START, details="hi")
    finally:
        reset_task_context(token)
        writer.close()
    rec = next(iter(load_jsonl(p)))
    assert isinstance(rec, TraceArch)
    assert rec.details == "hi"


def test_task_context_filter_does_not_inject_writer() -> None:
    from bitgn_contest_agent.arch_log import (
        TaskContextFilter, set_task_context, reset_task_context,
    )
    from bitgn_contest_agent.trace_writer import TraceWriter
    token = set_task_context(
        task_id="t1", run_index=0, trace_name="t.jsonl",
        writer=TraceWriter(path=__import__("tempfile").mktemp(suffix=".jsonl")),
    )
    try:
        f = TaskContextFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        assert not hasattr(rec, "writer")
    finally:
        reset_task_context(token)


def test_update_task_context_merges_fields() -> None:
    from bitgn_contest_agent.arch_log import (
        TaskContextFilter, set_task_context, reset_task_context,
        update_task_context,
    )
    token = set_task_context(task_id="t100", run_index=0, trace_name="t.jsonl")
    try:
        update_task_context(skill="finance-lookup", category="FINANCE_LOOKUP")
        f = TaskContextFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        assert rec.skill == "finance-lookup"
        assert rec.category == "FINANCE_LOOKUP"
        assert rec.task_id == "t100"  # original fields preserved
    finally:
        reset_task_context(token)
