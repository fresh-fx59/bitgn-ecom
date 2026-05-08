# tests/test_agent_arch_logging.py
"""End-to-end: per-task .log file + arch records in JSONL."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bitgn_contest_agent.arch_constants import ArchCategory
from bitgn_contest_agent.arch_log import (
    TaskContextFilter,
    emit_arch,
    reset_task_context,
    set_task_context,
)
from bitgn_contest_agent.trace_writer import TraceWriter
from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl


def test_per_task_log_file_captures_arch_lines(tmp_path) -> None:
    """Per-task FileHandler installed around the worker body collects
    arch lines into <trace>.log next to the JSONL."""
    import threading

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    trace_path = log_dir / "t100__run0.jsonl"
    log_path = trace_path.with_suffix(".log")
    writer = TraceWriter(path=trace_path)

    # Install context filter globally (mimicking cli.main).
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.INFO)
    ctx_filter = TaskContextFilter()
    root.addFilter(ctx_filter)
    fmt = logging.Formatter(
        "task=%(task_id)s run=%(run_index)s skill=%(skill)s "
        "category=%(category)s trace=%(trace_name)s %(message)s"
    )
    try:
        token = set_task_context(
            task_id="t100", run_index=0,
            trace_name="t100__run0.jsonl", writer=writer,
        )
        try:
            # Per-task handler, thread-filtered.
            tid = threading.get_ident()
            handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
            handler.setFormatter(fmt)
            handler.addFilter(lambda r: r.thread == tid)
            handler.addFilter(TaskContextFilter())
            root.addHandler(handler)
            try:
                emit_arch(
                    category=ArchCategory.SKILL_ROUTER,
                    skill="finance-lookup",
                    confidence=0.9,
                )
                emit_arch(
                    category=ArchCategory.VALIDATOR_T1,
                    at_step=2,
                    details="rule=mutation_guard",
                )
            finally:
                root.removeHandler(handler)
                handler.close()
        finally:
            reset_task_context(token)
            writer.close()
    finally:
        root.removeFilter(ctx_filter)
        root.setLevel(prev_level)

    # JSONL has TraceArch records
    arch = [r for r in load_jsonl(trace_path) if isinstance(r, TraceArch)]
    assert len(arch) == 2

    # .log file exists and contains self-identifying lines
    text = log_path.read_text(encoding="utf-8")
    assert "task=t100" in text
    assert "run=0" in text
    assert "trace=t100__run0.jsonl" in text
    assert "[ARCH:SKILL_ROUTER]" in text
    assert "[ARCH:VALIDATOR_T1]" in text


def test_two_concurrent_tasks_do_not_cross_contaminate(tmp_path) -> None:
    """Filter by thread isolates per-task logs."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.INFO)
    ctx_filter = TaskContextFilter()
    root.addFilter(ctx_filter)
    fmt = logging.Formatter(
        "task=%(task_id)s %(message)s"
    )

    def worker(task_id: str) -> Path:
        trace_path = log_dir / f"{task_id}__run0.jsonl"
        log_path = trace_path.with_suffix(".log")
        writer = TraceWriter(path=trace_path)
        tid = threading.get_ident()
        handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
        handler.setFormatter(fmt)
        handler.addFilter(lambda r: r.thread == tid)
        handler.addFilter(TaskContextFilter())
        root.addHandler(handler)
        token = set_task_context(
            task_id=task_id, run_index=0,
            trace_name=f"{task_id}__run0.jsonl", writer=writer,
        )
        try:
            emit_arch(category=ArchCategory.TASK_START, details=task_id)
        finally:
            reset_task_context(token)
            root.removeHandler(handler)
            handler.close()
            writer.close()
        return log_path

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            paths = list(ex.map(worker, ["tA", "tB"]))
    finally:
        root.removeFilter(ctx_filter)
        root.setLevel(prev_level)

    text_a = paths[0].read_text(encoding="utf-8")
    text_b = paths[1].read_text(encoding="utf-8")
    assert "tA" in text_a and "tB" not in text_a
    assert "tB" in text_b and "tA" not in text_b
