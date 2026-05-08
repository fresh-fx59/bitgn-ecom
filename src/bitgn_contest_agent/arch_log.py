# src/bitgn_contest_agent/arch_log.py
"""Central arch observability: emit, formatter, ContextVar filter.

emit_arch writes a TraceArch record to the task's JSONL (if writer
given) and to the root logger. _format_arch_line derives the log
text from the record itself so the two cannot drift.

The ContextVar-backed TaskContextFilter injects per-task identifiers
(task_id, run_index, trace_name, skill, category) onto every
LogRecord, so any log line is self-describing — grep by any field.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Optional

from bitgn_contest_agent.arch_constants import ArchCategory
from bitgn_contest_agent.trace_schema import TraceArch
from bitgn_contest_agent.trace_writer import TraceWriter

_LOG = logging.getLogger(__name__)

_CONTEXT_DEFAULTS: dict[str, Any] = {
    "task_id": "-",
    "run_index": "-",
    "trace_name": "-",
    "skill": "-",
    "category": "-",
    "writer": None,
}

_task_ctx: ContextVar[dict[str, Any]] = ContextVar(
    "bitgn_task_ctx", default=_CONTEXT_DEFAULTS,
)


def set_task_context(
    *, task_id: str, run_index: int, trace_name: str,
    skill: str = "-", category: str = "-",
    writer: Optional[TraceWriter] = None,
) -> Token:
    """Install task-scoped context for this worker. Returns a token
    that MUST be passed to reset_task_context() in a finally."""
    ctx = {
        "task_id": task_id,
        "run_index": run_index,
        "trace_name": trace_name,
        "skill": skill,
        "category": category,
        "writer": writer,
    }
    return _task_ctx.set(ctx)


def reset_task_context(token: Token) -> None:
    _task_ctx.reset(token)


def update_task_context(**fields: Any) -> None:
    """Merge fields into the current task context (e.g., after router
    decides). Safe when no task context is active — becomes a no-op
    on the default dict."""
    ctx = _task_ctx.get()
    if ctx is _CONTEXT_DEFAULTS:
        return
    ctx.update(fields)


def current_writer() -> Optional[TraceWriter]:
    ctx = _task_ctx.get()
    return ctx.get("writer")


class TaskContextFilter(logging.Filter):
    """Populates LogRecord with task context from _task_ctx."""

    _LOG_ATTRS = ("task_id", "run_index", "trace_name", "skill", "category")

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _task_ctx.get()
        for key in self._LOG_ATTRS:
            setattr(record, key, ctx.get(key, "-"))
        return True


_SERIALIZE_ORDER: tuple[str, ...] = (
    "at_step", "tier", "rule", "trigger", "result",
    "skill", "source", "confidence", "reasons", "details",
)


def _format_arch_line(rec: TraceArch) -> str:
    """Render [ARCH:CATEGORY] key=val ... — derived from the record,
    never duplicated in a format string."""
    parts: list[str] = [f"[ARCH:{rec.category.value}]"]
    data = rec.model_dump(mode="json", exclude_none=True)
    for key in _SERIALIZE_ORDER:
        if key in data and key != "category" and key != "kind":
            val = data[key]
            if key == "at_step":
                parts.append(f"step={val}")
            elif isinstance(val, float):
                parts.append(f"{key}={val:.2f}")
            elif isinstance(val, list):
                parts.append(f"{key}={val}")
            else:
                parts.append(f"{key}={val}")
    return " ".join(parts)


def emit_arch(
    writer: Optional[TraceWriter] = None,
    *,
    category: ArchCategory,
    at_step: Optional[int] = None,
    **fields: Any,
) -> None:
    """Emit an architecture event: writes to both JSONL (if writer)
    and stderr via the root logger. Single source of truth — the log
    line text is derived from the TraceArch record."""
    if writer is None:
        writer = current_writer()
    record = TraceArch(
        category=category,
        at_step=at_step,
        emitted_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **fields,
    )
    if writer is not None:
        writer.append_arch(record)
    _LOG.info("%s", _format_arch_line(record))
