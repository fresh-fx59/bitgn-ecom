# Architecture Observability Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `[ARCH:*]` architecture decisions automatically observable — structured JSONL records per-task plus a per-task stderr `.log` file — with enum-driven consistency across logic, schema, log format, and analyser tools.

**Architecture:** A new `arch_constants.py` module exposes `StrEnum`s for every categorical value (`ArchCategory`, `ValidatorT1Rule`, `ValidatorT2Trigger`, `ArchResult`, `RouterSource`). A single `emit_arch(**fields)` helper writes each event to both a `TraceArch` record (new additive JSONL kind) and the root logger; the root logger is teed per-task to `{trace_path}.log` via a `FileHandler` with a thread filter. A `ContextVar`-backed `TaskContextFilter` adds `task_id`, `run_index`, `trace_name`, `skill`, `category` to every log line.

**Tech Stack:** Python 3.14 (`StrEnum` from 3.11+), Pydantic v2 (existing), `contextvars` (stdlib), `logging` (stdlib). No new dependencies.

---

## Spec

See `docs/superpowers/specs/2026-04-14-arch-logging-design.md` for the approved design.

## File Structure

| File | Responsibility |
|------|----------------|
| `src/bitgn_contest_agent/arch_constants.py` **[create]** | Enum source of truth (ArchCategory, ValidatorT1Rule, ValidatorT2Trigger, ArchResult, RouterSource) |
| `src/bitgn_contest_agent/arch_log.py` **[create]** | `emit_arch()`, `_format_arch_line()`, `TaskContextFilter`, `_task_ctx` ContextVar, `set_task_context()`/`reset_task_context()`, `update_task_context()` |
| `src/bitgn_contest_agent/trace_schema.py` **[modify]** | Add `TraceArch`; extend `TraceMeta` with `intent_head: Optional[str]`; register in `_KIND_TO_MODEL` and `TraceRecord` union |
| `src/bitgn_contest_agent/trace_writer.py` **[modify]** | Add `append_arch(record: TraceArch)` method |
| `src/bitgn_contest_agent/validator.py` **[modify]** | Replace 10 `_LOG.info("[ARCH:...]",...)` sites with `emit_arch(...)` calls using enums |
| `src/bitgn_contest_agent/agent.py` **[modify]** | Replace 7 `_LOG.info("[ARCH:...]",...)` sites with `emit_arch(...)`; update ContextVar with skill/category after router decision |
| `src/bitgn_contest_agent/cli.py` **[modify]** | Install `TaskContextFilter` in `main()`; per-task `FileHandler` and ContextVar token in `_run_single_task` |
| `scripts/bench_summary.py` **[modify]** | Recognise `TraceArch` records; set `arch_present: bool` per task |
| `scripts/arch_report.py` **[create]** | CLI tool for reading arch timelines with enum-typed filters |
| `tests/test_arch_constants.py` **[create]** | Enum membership invariants |
| `tests/test_arch_log.py` **[create]** | `emit_arch`, formatter, ContextVar filter |
| `tests/test_trace_schema.py` **[modify]** | Add TraceArch round-trip + backward-compat tests |
| `tests/test_trace_writer.py` **[modify]** | Add `append_arch` test |
| `tests/test_validator.py` **[modify]** | Assert emit_arch called (or writer receives arch records) |
| `tests/test_agent_arch_logging.py` **[create]** | Integration: per-task log file, no cross-contamination |
| `tests/test_arch_report.py` **[create]** | CLI tool output and filtering |
| `tests/test_bench_summary.py` **[modify]** | `arch_present` flag propagation |

---

## Task 1: Enum source of truth — `arch_constants.py`

**Files:**
- Create: `src/bitgn_contest_agent/arch_constants.py`
- Test: `tests/test_arch_constants.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arch_constants.py
"""Enums drive logic, schema, log strings, and analyser filters."""
from __future__ import annotations

from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ValidatorT1Rule,
    ValidatorT2Trigger,
    ArchResult,
    RouterSource,
)


def test_arch_category_members() -> None:
    names = {c.name for c in ArchCategory}
    assert names == {
        "SKILL_ROUTER", "REACTIVE", "VALIDATOR_T1", "VALIDATOR_T2",
        "TERMINAL", "TERMINAL_R4", "LOOP_NUDGE",
        "FORMAT_VALIDATOR", "BODY_PRESERVATION", "TASK_START",
    }


def test_arch_category_value_equals_name() -> None:
    for cat in ArchCategory:
        assert cat.value == cat.name


def test_validator_t1_rule_values() -> None:
    assert ValidatorT1Rule.CONTRADICTION_OK_NEG.value == "contradiction_ok_neg"
    assert ValidatorT1Rule.CONTRADICTION_CLAR_POS.value == "contradiction_clar_pos"
    assert ValidatorT1Rule.DANGEROUS_DENIED_TO_OK.value == "dangerous_denied_to_ok"
    assert ValidatorT1Rule.MUTATION_GUARD.value == "mutation_guard"


def test_validator_t2_trigger_values() -> None:
    assert ValidatorT2Trigger.FIRST_TRANSITION.value == "first_transition"
    assert ValidatorT2Trigger.CLARIFICATION.value == "clarification"
    assert ValidatorT2Trigger.INBOX_READ.value == "inbox_read"
    assert ValidatorT2Trigger.PROGRESS_CHECK.value == "progress_check"
    assert ValidatorT2Trigger.ENTITY_FINANCE_SEARCH.value == "entity_finance_search"


def test_arch_result_values() -> None:
    assert ArchResult.OK.value == "OK"
    assert ArchResult.CORRECTED.value == "CORRECTED"
    assert ArchResult.ACCEPT.value == "ACCEPT"
    assert ArchResult.REJECT.value == "REJECT"
    assert ArchResult.MISMATCH.value == "MISMATCH"
    assert ArchResult.CONSISTENT.value == "CONSISTENT"


def test_router_source_values() -> None:
    assert RouterSource.TIER1_REGEX.value == "tier1_regex"
    assert RouterSource.TIER2_LLM.value == "tier2_llm"
    assert RouterSource.NONE.value == "none"


def test_all_are_str_subclass() -> None:
    # StrEnum members are str — logs and JSON serialize without .value
    assert isinstance(ArchCategory.VALIDATOR_T1, str)
    assert f"{ArchCategory.VALIDATOR_T1}" == "VALIDATOR_T1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_arch_constants.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bitgn_contest_agent.arch_constants'`

- [ ] **Step 3: Implement the enum module**

```python
# src/bitgn_contest_agent/arch_constants.py
"""Architecture observability enums — single source of truth.

Shared by: logic branches (validator.py, agent.py), JSONL schema
(trace_schema.TraceArch), log line formatter (arch_log.py), and
analyser CLI (scripts/arch_report.py). Renaming a member here
propagates to every consumer.
"""
from __future__ import annotations

from enum import StrEnum


class ArchCategory(StrEnum):
    SKILL_ROUTER = "SKILL_ROUTER"
    REACTIVE = "REACTIVE"
    VALIDATOR_T1 = "VALIDATOR_T1"
    VALIDATOR_T2 = "VALIDATOR_T2"
    TERMINAL = "TERMINAL"
    TERMINAL_R4 = "TERMINAL_R4"
    LOOP_NUDGE = "LOOP_NUDGE"
    FORMAT_VALIDATOR = "FORMAT_VALIDATOR"
    BODY_PRESERVATION = "BODY_PRESERVATION"
    TASK_START = "TASK_START"


class ValidatorT1Rule(StrEnum):
    CONTRADICTION_OK_NEG = "contradiction_ok_neg"
    CONTRADICTION_CLAR_POS = "contradiction_clar_pos"
    DANGEROUS_DENIED_TO_OK = "dangerous_denied_to_ok"
    MUTATION_GUARD = "mutation_guard"


class ValidatorT2Trigger(StrEnum):
    FIRST_TRANSITION = "first_transition"
    CLARIFICATION = "clarification"
    INBOX_READ = "inbox_read"
    PROGRESS_CHECK = "progress_check"
    ENTITY_FINANCE_SEARCH = "entity_finance_search"


class ArchResult(StrEnum):
    OK = "OK"
    CORRECTED = "CORRECTED"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    MISMATCH = "MISMATCH"
    CONSISTENT = "CONSISTENT"


class RouterSource(StrEnum):
    TIER1_REGEX = "tier1_regex"
    TIER2_LLM = "tier2_llm"
    NONE = "none"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_arch_constants.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/arch_constants.py tests/test_arch_constants.py
git commit -m "feat(arch-log): enum source of truth for arch observability"
```

---

## Task 2: `TraceArch` record type + schema extension

**Files:**
- Modify: `src/bitgn_contest_agent/trace_schema.py`
- Test: `tests/test_trace_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trace_schema.py`:

```python
def test_trace_arch_minimal() -> None:
    from bitgn_contest_agent.trace_schema import TraceArch
    from bitgn_contest_agent.arch_constants import ArchCategory
    rec = TraceArch(category=ArchCategory.SKILL_ROUTER)
    assert rec.kind == "arch"
    assert rec.at_step is None  # pre-task convention
    assert rec.category == ArchCategory.SKILL_ROUTER
    assert rec.rule is None
    assert rec.trigger is None


def test_trace_arch_full_fields() -> None:
    from bitgn_contest_agent.trace_schema import TraceArch
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT1Rule, ArchResult, RouterSource
    )
    rec = TraceArch(
        category=ArchCategory.VALIDATOR_T1,
        at_step=3,
        rule=ValidatorT1Rule.MUTATION_GUARD,
        result=ArchResult.OK,
        source=RouterSource.TIER1_REGEX,
        confidence=0.87,
        details="tool=write",
        emitted_at="2026-04-14T18:22:31.104+00:00",
    )
    dumped = rec.model_dump(mode="json")
    assert dumped["category"] == "VALIDATOR_T1"
    assert dumped["rule"] == "mutation_guard"
    assert dumped["source"] == "tier1_regex"


def test_trace_arch_in_kind_map() -> None:
    from bitgn_contest_agent.trace_schema import _KIND_TO_MODEL, TraceArch
    assert _KIND_TO_MODEL["arch"] is TraceArch


def test_trace_arch_load_jsonl_roundtrip(tmp_path) -> None:
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT2Trigger, ArchResult
    )
    p = tmp_path / "t.jsonl"
    rec = TraceArch(
        category=ArchCategory.VALIDATOR_T2,
        at_step=5,
        trigger=ValidatorT2Trigger.FIRST_TRANSITION,
        result=ArchResult.CORRECTED,
    )
    p.write_text(json.dumps(rec.model_dump(mode="json")) + "\n", encoding="utf-8")
    read = list(load_jsonl(p))
    assert len(read) == 1
    assert isinstance(read[0], TraceArch)
    assert read[0].trigger == ValidatorT2Trigger.FIRST_TRANSITION
    assert read[0].result == ArchResult.CORRECTED


def test_trace_meta_intent_head_optional() -> None:
    # Backward-compat: existing traces without intent_head still parse.
    from bitgn_contest_agent.trace_schema import TraceMeta
    m = TraceMeta(
        agent_version="0.1.8", agent_commit="abc",
        model="gpt-5.3-codex", backend="openai_compat",
        reasoning_effort="medium", benchmark="bitgn/pac1",
        task_id="t100", task_index=99,
        started_at="2026-04-14T18:22:31+00:00",
        trace_schema_version="1.0.0",
    )
    assert m.intent_head is None


def test_trace_meta_with_intent_head() -> None:
    from bitgn_contest_agent.trace_schema import TraceMeta
    m = TraceMeta(
        agent_version="0.1.8", agent_commit="abc",
        model="gpt-5.3-codex", backend="openai_compat",
        reasoning_effort="medium", benchmark="bitgn/pac1",
        task_id="t100", task_index=99,
        started_at="2026-04-14T18:22:31+00:00",
        trace_schema_version="1.0.0",
        intent_head="how much did I pay to Filamenthütte Wien in total?",
    )
    assert m.intent_head.startswith("how much")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_trace_schema.py -v -k "arch or intent"`
Expected: FAIL — `TraceArch` not importable, `intent_head` rejected by `TraceMeta`.

- [ ] **Step 3: Add `TraceArch` and extend `TraceMeta`**

Modify `src/bitgn_contest_agent/trace_schema.py`:

After the existing imports, add:
```python
from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ValidatorT1Rule,
    ValidatorT2Trigger,
    ArchResult,
    RouterSource,
)
```

Add `intent_head` to `TraceMeta` (after `harness_url`):
```python
class TraceMeta(_BaseRecord):
    kind: Literal["meta"] = "meta"
    agent_version: str
    agent_commit: str
    model: str
    backend: str
    reasoning_effort: str
    benchmark: str
    task_id: str
    task_index: int
    started_at: str
    trace_schema_version: str
    harness_url: Optional[str] = None
    cancelled: bool = False
    intent_head: Optional[str] = None  # first 240 chars of task_text
```

Add new record class after `TraceEvent`:
```python
class TraceArch(_BaseRecord):
    kind: Literal["arch"] = "arch"
    at_step: Optional[int] = None      # None = pre-task (router)
    category: ArchCategory
    tier: Optional[str] = None
    rule: Optional[ValidatorT1Rule] = None
    trigger: Optional[ValidatorT2Trigger] = None
    result: Optional[ArchResult] = None
    skill: Optional[str] = None
    source: Optional[RouterSource] = None
    confidence: Optional[float] = None
    reasons: Optional[List[str]] = None
    details: Optional[str] = None
    emitted_at: Optional[str] = None
```

Update `TraceRecord` union:
```python
TraceRecord = Union[
    TraceMeta, TraceTask, TracePrepass,
    TraceStep, TraceEvent, TraceArch, TraceOutcome,
]
```

Update `_KIND_TO_MODEL`:
```python
_KIND_TO_MODEL: dict[str, type[_BaseRecord]] = {
    "meta": TraceMeta,
    "task": TraceTask,
    "prepass": TracePrepass,
    "step": TraceStep,
    "event": TraceEvent,
    "arch": TraceArch,
    "outcome": TraceOutcome,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trace_schema.py -v`
Expected: PASS (all tests, including new ones)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/trace_schema.py tests/test_trace_schema.py
git commit -m "feat(arch-log): TraceArch record + TraceMeta.intent_head"
```

---

## Task 3: `TraceWriter.append_arch`

**Files:**
- Modify: `src/bitgn_contest_agent/trace_writer.py`
- Test: `tests/test_trace_writer.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trace_writer.py`:

```python
def test_append_arch_writes_record(tmp_path) -> None:
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT1Rule
    )
    p = tmp_path / "t.jsonl"
    w = TraceWriter(path=p)
    w.append_arch(TraceArch(
        category=ArchCategory.VALIDATOR_T1,
        at_step=2,
        rule=ValidatorT1Rule.MUTATION_GUARD,
        details="tool=write",
    ))
    w.close()
    records = list(load_jsonl(p))
    assert len(records) == 1
    assert isinstance(records[0], TraceArch)
    assert records[0].rule == ValidatorT1Rule.MUTATION_GUARD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trace_writer.py::test_append_arch_writes_record -v`
Expected: FAIL — `TraceWriter` has no `append_arch`.

- [ ] **Step 3: Add `append_arch` to `TraceWriter`**

In `src/bitgn_contest_agent/trace_writer.py`, add to imports:
```python
from bitgn_contest_agent.trace_schema import (
    StepLLMStats,
    StepSessionAfter,
    StepToolResult,
    TraceArch,
    TraceEvent,
    TraceMeta,
    TraceOutcome,
    TracePrepass,
    TraceStep,
    TraceTask,
)
```

Add new method after `append_event`:
```python
def append_arch(self, record: TraceArch) -> None:
    """Write a TraceArch record (architecture decision event)."""
    self._write(record.model_dump(mode="json"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_trace_writer.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/trace_writer.py tests/test_trace_writer.py
git commit -m "feat(arch-log): TraceWriter.append_arch"
```

---

## Task 4: `emit_arch` helper, `_format_arch_line`, `TaskContextFilter`, ContextVar

**Files:**
- Create: `src/bitgn_contest_agent/arch_log.py`
- Test: `tests/test_arch_log.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_arch_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bitgn_contest_agent.arch_log'`

- [ ] **Step 3: Implement `arch_log.py`**

```python
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
}

_task_ctx: ContextVar[dict[str, Any]] = ContextVar(
    "bitgn_task_ctx", default=_CONTEXT_DEFAULTS,
)


def set_task_context(
    *, task_id: str, run_index: int, trace_name: str,
    skill: str = "-", category: str = "-",
) -> Token:
    """Install task-scoped context for this worker. Returns a token
    that MUST be passed to reset_task_context() in a finally."""
    ctx = {
        "task_id": task_id,
        "run_index": run_index,
        "trace_name": trace_name,
        "skill": skill,
        "category": category,
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


class TaskContextFilter(logging.Filter):
    """Populates LogRecord with task context from _task_ctx."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _task_ctx.get()
        for key, default in _CONTEXT_DEFAULTS.items():
            setattr(record, key, ctx.get(key, default))
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
    writer: Optional[TraceWriter],
    *,
    category: ArchCategory,
    at_step: Optional[int] = None,
    **fields: Any,
) -> None:
    """Emit an architecture event: writes to both JSONL (if writer)
    and stderr via the root logger. Single source of truth — the log
    line text is derived from the TraceArch record."""
    record = TraceArch(
        category=category,
        at_step=at_step,
        emitted_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **fields,
    )
    if writer is not None:
        writer.append_arch(record)
    _LOG.info("%s", _format_arch_line(record))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_arch_log.py -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/arch_log.py tests/test_arch_log.py
git commit -m "feat(arch-log): emit_arch + ContextVar + TaskContextFilter"
```

---

## Task 5: Replace arch log sites in `validator.py`

**Files:**
- Modify: `src/bitgn_contest_agent/validator.py`
- Test: `tests/test_validator.py`

The validator needs a `writer` reference to call `emit_arch(writer, ...)`. Rather than threading it through every method, we read it from a new ContextVar exposed by `arch_log`.

- [ ] **Step 1: Add `writer` to task context**

Modify `src/bitgn_contest_agent/arch_log.py` — extend context to include an optional writer:

```python
_CONTEXT_DEFAULTS: dict[str, Any] = {
    "task_id": "-",
    "run_index": "-",
    "trace_name": "-",
    "skill": "-",
    "category": "-",
    "writer": None,
}
```

Update `set_task_context` signature:
```python
def set_task_context(
    *, task_id: str, run_index: int, trace_name: str,
    skill: str = "-", category: str = "-",
    writer: Optional[TraceWriter] = None,
) -> Token:
    ctx = {
        "task_id": task_id,
        "run_index": run_index,
        "trace_name": trace_name,
        "skill": skill,
        "category": category,
        "writer": writer,
    }
    return _task_ctx.set(ctx)
```

Add a thin helper and simplify `emit_arch` to read the writer from context when no explicit writer is passed:
```python
def current_writer() -> Optional[TraceWriter]:
    ctx = _task_ctx.get()
    return ctx.get("writer")


def emit_arch(
    writer: Optional[TraceWriter] = None,
    *,
    category: ArchCategory,
    at_step: Optional[int] = None,
    **fields: Any,
) -> None:
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
```

And update `TaskContextFilter.filter` to NOT inject `writer` onto LogRecord (writer is internal, not a log field):
```python
class TaskContextFilter(logging.Filter):
    _LOG_ATTRS = ("task_id", "run_index", "trace_name", "skill", "category")

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _task_ctx.get()
        for key in self._LOG_ATTRS:
            setattr(record, key, ctx.get(key, "-"))
        return True
```

- [ ] **Step 2: Update arch_log tests**

Append to `tests/test_arch_log.py`:

```python
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
```

Run: `.venv/bin/python -m pytest tests/test_arch_log.py -v` — Expected: PASS.

- [ ] **Step 3: Write failing tests for validator arch events**

Append to `tests/test_validator.py`:

```python
def test_validator_t1_rule_emits_arch_record(tmp_path) -> None:
    """Tier 1 mutation_guard rule writes a TraceArch via context writer."""
    from bitgn_contest_agent.arch_log import set_task_context, reset_task_context
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT1Rule,
    )
    from bitgn_contest_agent.validator import StepValidator
    from bitgn_contest_agent.schemas import NextStep
    from bitgn_contest_agent.session import Session

    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t1", run_index=0, trace_name="t.jsonl", writer=writer,
    )
    try:
        v = StepValidator()
        step = NextStep.model_validate({
            "current_state": "writing while gathering",
            "plan_remaining_steps_brief": ["write"],
            "function": {"tool": "write", "path": "/foo.md", "content": "x"},
            "observation": "about to write",
            "outcome_leaning": "GATHERING_INFORMATION",
        })
        sess = Session()
        v.check_step(step, sess, step_idx=2, max_steps=20,
                     reactive_injected_this_step=False)
    finally:
        reset_task_context(token)
        writer.close()

    arch = [r for r in load_jsonl(p) if isinstance(r, TraceArch)]
    assert any(
        r.category == ArchCategory.VALIDATOR_T1
        and r.rule == ValidatorT1Rule.MUTATION_GUARD
        for r in arch
    )


def test_validator_terminal_emits_arch_record(tmp_path) -> None:
    from bitgn_contest_agent.arch_log import set_task_context, reset_task_context
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import ArchCategory, ArchResult
    from bitgn_contest_agent.validator import StepValidator
    from bitgn_contest_agent.schemas import NextStep
    from bitgn_contest_agent.session import Session

    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t1", run_index=0, trace_name="t.jsonl", writer=writer,
    )
    try:
        v = StepValidator()
        step = NextStep.model_validate({
            "current_state": "done",
            "plan_remaining_steps_brief": [],
            "function": {
                "tool": "report_task_completion",
                "outcome": "OUTCOME_OK",
                "final_answer": "42",
                "grounding_refs": [],
            },
            "observation": "found it",
            "outcome_leaning": "OUTCOME_OK",
        })
        sess = Session()
        v.check_terminal(sess, step)
    finally:
        reset_task_context(token)
        writer.close()

    arch = [r for r in load_jsonl(p) if isinstance(r, TraceArch)]
    assert any(
        r.category == ArchCategory.TERMINAL and r.result == ArchResult.ACCEPT
        for r in arch
    )
```

Run: `.venv/bin/python -m pytest tests/test_validator.py -v -k "arch_record"`
Expected: FAIL — validator still writes free-form log strings, no TraceArch records produced.

- [ ] **Step 4: Replace validator call sites**

In `src/bitgn_contest_agent/validator.py`, add imports at top:

```python
from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ArchResult,
    ValidatorT1Rule,
    ValidatorT2Trigger,
)
from bitgn_contest_agent.arch_log import emit_arch
```

Replace each `_LOG.info("[ARCH:...]", ...)` exactly as follows.

**Terminal (around line 144):**

Before:
```python
if reasons:
    _LOG.info("[ARCH:TERMINAL] verdict=REJECT reasons=%s", reasons)
else:
    _LOG.info("[ARCH:TERMINAL] verdict=ACCEPT outcome=%s", fn.outcome)
```

After:
```python
if reasons:
    emit_arch(
        category=ArchCategory.TERMINAL,
        result=ArchResult.REJECT,
        reasons=list(reasons),
    )
else:
    emit_arch(
        category=ArchCategory.TERMINAL,
        result=ArchResult.ACCEPT,
        details=f"outcome={fn.outcome}",
    )
```

**Tier 1 rules (lines ~162, 171, 183, 192):**

Replace each line, for example:
```python
# Before
_LOG.info("[ARCH:VALIDATOR_T1] rule=contradiction_ok_neg step=%d leaning=%s", step_idx, leaning)
# After
emit_arch(
    category=ArchCategory.VALIDATOR_T1,
    at_step=step_idx,
    rule=ValidatorT1Rule.CONTRADICTION_OK_NEG,
    details=f"leaning={leaning}",
)
```

Do the same for:
- `contradiction_clar_pos` → `ValidatorT1Rule.CONTRADICTION_CLAR_POS`
- `dangerous_denied_to_ok` → `ValidatorT1Rule.DANGEROUS_DENIED_TO_OK`
- `mutation_guard` → `ValidatorT1Rule.MUTATION_GUARD` (add `details=f"tool={tool}"`)

**Tier 2 triggers (lines ~226-280):**

Each trigger has two log lines (fire + result). Replace pairs:

```python
# Before (trigger 1 fire)
_LOG.info("[ARCH:VALIDATOR_T2] trigger=first_transition step=%d leaning=%s", step_idx, leaning)
# After
emit_arch(
    category=ArchCategory.VALIDATOR_T2,
    at_step=step_idx,
    trigger=ValidatorT2Trigger.FIRST_TRANSITION,
    details=f"leaning={leaning}",
)
```

```python
# Before (result)
_LOG.info("[ARCH:VALIDATOR_T2] trigger=first_transition result=%s", "CORRECTED" if result else "OK")
# After
emit_arch(
    category=ArchCategory.VALIDATOR_T2,
    at_step=step_idx,
    trigger=ValidatorT2Trigger.FIRST_TRANSITION,
    result=ArchResult.CORRECTED if result else ArchResult.OK,
)
```

Apply the same pattern for triggers 2, 3, 4, 5 (`CLARIFICATION`, `INBOX_READ`, `PROGRESS_CHECK`, `ENTITY_FINANCE_SEARCH`).

For `progress_check`'s fire line, preserve the step ratio in `details`:
```python
emit_arch(
    category=ArchCategory.VALIDATOR_T2,
    at_step=step_idx,
    trigger=ValidatorT2Trigger.PROGRESS_CHECK,
    details=f"step={step_idx}/{max_steps} leaning={leaning}",
)
```

For `entity_finance_search`'s fire line, put the pattern in details:
```python
emit_arch(
    category=ArchCategory.VALIDATOR_T2,
    at_step=step_idx,
    trigger=ValidatorT2Trigger.ENTITY_FINANCE_SEARCH,
    details=f"pattern={fn_pattern}",
)
```

**Terminal R4 (around lines 458, 466):**

Before:
```python
_LOG.info("[ARCH:TERMINAL_R4] result=MISMATCH actual=%d conf=%.2f detail=%s",
          len(actual), conf, detail)
...
_LOG.info("[ARCH:TERMINAL_R4] result=%s actual=%d conf=%.2f", cat, len(actual), conf)
```

After:
```python
emit_arch(
    category=ArchCategory.TERMINAL_R4,
    result=ArchResult.MISMATCH,
    confidence=conf,
    details=f"actual={len(actual)} detail={detail}",
)
...
emit_arch(
    category=ArchCategory.TERMINAL_R4,
    result=ArchResult(cat),  # cat is "CONSISTENT" or "MISMATCH" string
    confidence=conf,
    details=f"actual={len(actual)}",
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_validator.py -v`
Expected: PASS (all tests, including new arch_record tests)

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/validator.py src/bitgn_contest_agent/arch_log.py tests/test_validator.py tests/test_arch_log.py
git commit -m "refactor(arch-log): validator uses emit_arch instead of free-form strings"
```

---

## Task 6: Replace arch log sites in `agent.py`; push router skill/category into ContextVar

**Files:**
- Modify: `src/bitgn_contest_agent/agent.py`
- Test: `tests/test_agent_router_injection.py`, `tests/test_agent_reactive_injection.py`

- [ ] **Step 1: Write failing test for router context update**

Append to `tests/test_agent_router_injection.py`:

```python
def test_router_decision_updates_task_context_skill_and_category(tmp_path) -> None:
    """After router fires, skill+category are injected into ContextVar."""
    import logging
    from bitgn_contest_agent.arch_log import (
        set_task_context, reset_task_context, TaskContextFilter,
    )
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.agent import build_initial_messages
    from bitgn_contest_agent.router import load_router
    from pathlib import Path

    skills_dir = (Path(__file__).parent.parent
                  / "src" / "bitgn_contest_agent" / "skills")
    router = load_router(skills_dir=skills_dir)
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="t100", run_index=0, trace_name="t100__run0.jsonl",
        writer=writer,
    )
    try:
        build_initial_messages(
            task_text=(
                "How much did Müller charge me for pen refills 51 days ago?"
            ),
            task_id="t100",
            router=router,
        )
        f = TaskContextFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        assert rec.skill == "finance-lookup"
        assert rec.category == "FINANCE_LOOKUP"
    finally:
        reset_task_context(token)
        writer.close()


def test_router_no_match_sets_category_unknown(tmp_path) -> None:
    import logging
    from bitgn_contest_agent.arch_log import (
        set_task_context, reset_task_context, TaskContextFilter,
    )
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.agent import build_initial_messages
    from bitgn_contest_agent.router import load_router
    from pathlib import Path

    skills_dir = (Path(__file__).parent.parent
                  / "src" / "bitgn_contest_agent" / "skills")
    router = load_router(skills_dir=skills_dir)
    p = tmp_path / "t.jsonl"
    writer = TraceWriter(path=p)
    token = set_task_context(
        task_id="tx", run_index=0, trace_name="tx.jsonl", writer=writer,
    )
    try:
        build_initial_messages(
            task_text="handle the next inbox item please",
            task_id="tx",
            router=router,
        )
        f = TaskContextFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        f.filter(rec)
        # No finance match → skill stays "-", category is "UNKNOWN"
        assert rec.skill == "-"
        assert rec.category == "UNKNOWN"
    finally:
        reset_task_context(token)
        writer.close()
```

Run: `.venv/bin/python -m pytest tests/test_agent_router_injection.py -v -k "context"`
Expected: FAIL — context isn't updated yet.

- [ ] **Step 2: Replace router/reactive/etc. call sites in agent.py**

At top of `src/bitgn_contest_agent/agent.py`, add imports:

```python
from bitgn_contest_agent.arch_constants import (
    ArchCategory,
    ArchResult,
    RouterSource,
)
from bitgn_contest_agent.arch_log import emit_arch, update_task_context
```

**Router block (around line 141-157):**

Before:
```python
if decision.skill_name is not None:
    body = router.skill_body_for(decision.skill_name)
    if body is not None:
        _LOG.info("[ARCH:SKILL_ROUTER] task=%s skill=%s source=%s conf=%.2f vars=%s",
                  task_id, decision.skill_name, decision.source,
                  decision.confidence, decision.extracted)
        prefix = (...)
        messages.append(Message(role="user", content=prefix + body))
else:
    _LOG.info("[ARCH:SKILL_ROUTER] task=%s skill=none", task_id)
```

After:
```python
if decision.skill_name is not None:
    body = router.skill_body_for(decision.skill_name)
    if body is not None:
        emit_arch(
            category=ArchCategory.SKILL_ROUTER,
            skill=decision.skill_name,
            source=RouterSource(decision.source),
            confidence=decision.confidence,
            details=f"vars={decision.extracted}",
        )
        update_task_context(
            skill=decision.skill_name,
            category=decision.category or "UNKNOWN",
        )
        prefix = (
            f"SKILL CONTEXT (router-injected): {decision.skill_name}\n"
            f"Captured variables: {_json.dumps(decision.extracted)}\n\n"
        )
        messages.append(Message(role="user", content=prefix + body))
else:
    emit_arch(category=ArchCategory.SKILL_ROUTER, source=RouterSource.NONE)
    update_task_context(skill="-", category=decision.category or "UNKNOWN")
```

**Terminal reject (around line 301):**

Before:
```python
_LOG.info("[ARCH:TERMINAL] step=%d action=reject reasons=%s", step_idx, verdict.reasons)
```

After (leave as emit_arch only if the terminal ACCEPT/REJECT isn't already covered by validator.check_terminal() — which it is. So this line becomes a step-scoped annotation):
```python
emit_arch(
    category=ArchCategory.TERMINAL,
    at_step=step_idx,
    result=ArchResult.REJECT,
    details="action=reject",
    reasons=list(verdict.reasons),
)
```

**Loop nudge (around line 370):**

Before:
```python
_LOG.info("[ARCH:LOOP_NUDGE] step=%d call=%s", step_idx, call_tuple)
```

After:
```python
emit_arch(
    category=ArchCategory.LOOP_NUDGE,
    at_step=step_idx,
    details=f"call={call_tuple}",
)
```

**Format validator (around line 445):**

Before:
```python
_LOG.info("[ARCH:FORMAT_VALIDATOR] step=%d path=%s error=%s", step_idx, write_path, val_result.error)
```

After:
```python
emit_arch(
    category=ArchCategory.FORMAT_VALIDATOR,
    at_step=step_idx,
    details=f"path={write_path} error={val_result.error}",
)
```

**Body preservation (around line 484):**

Before:
```python
_LOG.info("[ARCH:BODY_PRESERVATION] step=%d path=%s old_len=%d new_len=%d", step_idx, write_path, len(old_body), len(new_body))
```

After:
```python
emit_arch(
    category=ArchCategory.BODY_PRESERVATION,
    at_step=step_idx,
    details=f"path={write_path} old_len={len(old_body)} new_len={len(new_body)}",
)
```

**Reactive (around line 519-522):**

Before:
```python
_LOG.info(
    "[ARCH:REACTIVE] step=%d skill=%s source=%s conf=%.2f trigger=%s(%s)",
    step_idx, reactive_decision.skill_name, reactive_decision.source,
    reactive_decision.confidence, getattr(fn, 'tool', ''), trigger_path,
)
```

After:
```python
emit_arch(
    category=ArchCategory.REACTIVE,
    at_step=step_idx,
    skill=reactive_decision.skill_name,
    source=RouterSource(reactive_decision.source),
    confidence=reactive_decision.confidence,
    details=f"trigger={getattr(fn, 'tool', '')}({trigger_path})",
)
```

**Validator correction notice (around line 542):**

Before:
```python
_LOG.info("[ARCH:VALIDATOR] step=%d correction=%s", step_idx, correction[:120])
```

Keep as-is (it's a downstream annotation, not a categorized decision). Leave the log line unchanged.

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest tests/test_agent_router_injection.py tests/test_agent_reactive_injection.py tests/test_agent_loop.py -v`
Expected: PASS (including new context tests)

- [ ] **Step 4: Commit**

```bash
git add src/bitgn_contest_agent/agent.py tests/test_agent_router_injection.py
git commit -m "refactor(arch-log): agent.py uses emit_arch + updates ContextVar with skill/category"
```

---

## Task 7: Wire CLI — install filter globally, set task context, per-task FileHandler

**Files:**
- Modify: `src/bitgn_contest_agent/cli.py`
- Test: `tests/test_agent_arch_logging.py` (new integration test)

- [ ] **Step 1: Write failing integration test**

```python
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

    text_a = paths[0].read_text(encoding="utf-8")
    text_b = paths[1].read_text(encoding="utf-8")
    assert "tA" in text_a and "tB" not in text_a
    assert "tB" in text_b and "tA" not in text_b
```

Run: `.venv/bin/python -m pytest tests/test_agent_arch_logging.py -v`
Expected: PASS — arch_log machinery from Task 4 already supports this; test confirms wiring patterns work.

- [ ] **Step 2: Install global filter + format in `main()`**

Modify `src/bitgn_contest_agent/cli.py`. Replace `logging.basicConfig(...)` in `main()`:

Before:
```python
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
```

After:
```python
def main(argv: list[str] | None = None) -> int:
    from bitgn_contest_agent.arch_log import TaskContextFilter
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "task=%(task_id)s run=%(run_index)s "
            "skill=%(skill)s category=%(category)s "
            "trace=%(trace_name)s "
            "%(name)s %(message)s"
        ),
    )
    # Install filter on every handler so the new %(task_id)s etc. fields
    # always resolve. Also install on root so records created ad-hoc get
    # the attributes.
    ctx_filter = TaskContextFilter()
    root = logging.getLogger()
    root.addFilter(ctx_filter)
    for h in root.handlers:
        h.addFilter(ctx_filter)
```

- [ ] **Step 3: Wire task-scoped writer/handler in `_run_single_task`**

In `src/bitgn_contest_agent/cli.py`, modify `_run_single_task`. After `writer = TraceWriter(path=trace_path)` and before `writer.write_meta(...)`:

```python
import threading as _threading
from bitgn_contest_agent.arch_log import (
    set_task_context,
    reset_task_context,
    emit_arch,
)
from bitgn_contest_agent.arch_constants import ArchCategory

# Per-task stderr log file, next to the JSONL.
task_log_path = trace_path.with_suffix(".log")
task_log_path.parent.mkdir(parents=True, exist_ok=True)
task_handler = logging.FileHandler(task_log_path, encoding="utf-8", delay=True)
task_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)s "
        "task=%(task_id)s run=%(run_index)s "
        "skill=%(skill)s category=%(category)s "
        "trace=%(trace_name)s "
        "%(name)s %(message)s"
    )
)
_worker_tid = _threading.get_ident()
task_handler.addFilter(lambda r: r.thread == _worker_tid)
from bitgn_contest_agent.arch_log import TaskContextFilter
task_handler.addFilter(TaskContextFilter())
root_logger = logging.getLogger()
root_logger.addHandler(task_handler)

ctx_token = set_task_context(
    task_id=effective_task_id,
    run_index=run_index,
    trace_name=trace_path.name,
    writer=writer,
)
```

Compute `intent_head` and write it into meta, emit `[ARCH:TASK_START]`:

Replace `writer.write_meta(TraceMeta(...))` with:
```python
intent_head = (started.instruction or "")[:240] if started.instruction else None
writer.write_meta(
    TraceMeta(
        agent_version=__version__,
        agent_commit=_git_commit_short(),
        model=cfg.model,
        backend="openai_compat",
        reasoning_effort=cfg.reasoning_effort,
        benchmark=cfg.benchmark,
        task_id=effective_task_id,
        task_index=task.task_index,
        started_at=datetime.now(timezone.utc).isoformat(),
        trace_schema_version=TRACE_SCHEMA_VERSION,
        harness_url=started.harness_url,
        intent_head=intent_head,
    )
)
emit_arch(
    category=ArchCategory.TASK_START,
    details=(intent_head or "")[:240],
)
```

Wrap the rest of the existing body in `try: ... finally:` that resets context and removes the handler. Locate the existing `try:` (line 179 in cli.py) that wraps the `start_trial`/`start_task` call — we need to extend the `finally` OR add a nested try.

The safest restructure: the existing function already has a `try/except` covering the body. Add a new outer `try/finally` after the writer + context are set. Final pattern:

```python
def _run_single_task(...) -> TaskExecutionResult:
    started: StartedTask | None = None
    writer: TraceWriter | None = None
    effective_task_id = task.task_id
    task_handler = None
    ctx_token = None
    root_logger = logging.getLogger()
    try:
        if task.trial_id is not None:
            started = harness.start_trial(task.trial_id)
        else:
            started = harness.start_task(task.task_id)
        effective_task_id = started.task_id

        adapter = PcmAdapter(
            runtime=started.runtime_client,
            max_tool_result_bytes=cfg.max_tool_result_bytes,
        )

        trace_path = _trace_path(cfg, run_id, effective_task_id, run_index)
        writer = TraceWriter(path=trace_path)

        # Install per-task log file + task context AFTER writer exists.
        task_log_path = trace_path.with_suffix(".log")
        task_handler = logging.FileHandler(
            task_log_path, encoding="utf-8", delay=True,
        )
        task_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s "
            "task=%(task_id)s run=%(run_index)s "
            "skill=%(skill)s category=%(category)s "
            "trace=%(trace_name)s "
            "%(name)s %(message)s"
        ))
        _worker_tid = _threading.get_ident()
        task_handler.addFilter(lambda r: r.thread == _worker_tid)
        task_handler.addFilter(TaskContextFilter())
        root_logger.addHandler(task_handler)

        ctx_token = set_task_context(
            task_id=effective_task_id,
            run_index=run_index,
            trace_name=trace_path.name,
            writer=writer,
        )

        intent_head = (started.instruction or "")[:240] if started.instruction else None
        writer.write_meta(TraceMeta(
            ...,  # existing fields
            intent_head=intent_head,
        ))
        emit_arch(
            category=ArchCategory.TASK_START,
            details=intent_head or "",
        )

        # ... existing loop/dispatch body stays the same ...
        return TaskExecutionResult(...)
    except Exception as exc:
        # existing crash handling stays
        ...
    finally:
        if ctx_token is not None:
            reset_task_context(ctx_token)
        if task_handler is not None:
            root_logger.removeHandler(task_handler)
            task_handler.close()
```

Add imports at the top of `cli.py`:
```python
import threading as _threading
from bitgn_contest_agent.arch_constants import ArchCategory
from bitgn_contest_agent.arch_log import (
    TaskContextFilter,
    emit_arch,
    reset_task_context,
    set_task_context,
)
```

- [ ] **Step 4: Run the integration test and existing CLI tests**

Run: `.venv/bin/python -m pytest tests/test_agent_arch_logging.py tests/test_cli*.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/cli.py tests/test_agent_arch_logging.py
git commit -m "feat(arch-log): per-task FileHandler + ContextVar wiring in CLI"
```

---

## Task 8: `bench_summary.py` propagates `arch_present` flag

**Files:**
- Modify: `scripts/bench_summary.py`
- Test: `tests/test_bench_summary.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_bench_summary.py` (find existing `def test_...`, pattern is pytest functions). Add:

```python
def test_bench_summary_reports_arch_present(tmp_path) -> None:
    import json as _json
    from pathlib import Path
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import (
        TraceMeta, TraceOutcome, StepLLMStats, StepSessionAfter,
        StepToolResult, TRACE_SCHEMA_VERSION,
    )
    from bitgn_contest_agent.arch_log import emit_arch
    from bitgn_contest_agent.arch_constants import ArchCategory
    from scripts.bench_summary import summarize  # type: ignore

    run_dir = tmp_path / "run1"
    run_dir.mkdir()

    # Task A: has arch records
    path_a = run_dir / "tA__run0.jsonl"
    w = TraceWriter(path=path_a)
    w.write_meta(TraceMeta(
        agent_version="x", agent_commit="y", model="m", backend="b",
        reasoning_effort="medium", benchmark="bench",
        task_id="tA", task_index=0,
        started_at="2026-04-14T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    ))
    emit_arch(w, category=ArchCategory.SKILL_ROUTER, skill="finance-lookup")
    w.append_outcome(TraceOutcome(
        terminated_by="report_completion", reported="OUTCOME_OK",
        enforcer_bypassed=False, total_steps=1, total_llm_calls=1,
        total_prompt_tokens=0, total_completion_tokens=0, score=1.0,
    ))
    w.close()

    # Task B: no arch records
    path_b = run_dir / "tB__run0.jsonl"
    w = TraceWriter(path=path_b)
    w.write_meta(TraceMeta(
        agent_version="x", agent_commit="y", model="m", backend="b",
        reasoning_effort="medium", benchmark="bench",
        task_id="tB", task_index=1,
        started_at="2026-04-14T00:00:00+00:00",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    ))
    w.append_outcome(TraceOutcome(
        terminated_by="report_completion", reported="OUTCOME_OK",
        enforcer_bypassed=False, total_steps=1, total_llm_calls=1,
        total_prompt_tokens=0, total_completion_tokens=0, score=1.0,
    ))
    w.close()

    summary = summarize(logs_dir=run_dir)
    assert summary["tasks"]["tA"]["arch_present"] is True
    assert summary["tasks"]["tB"]["arch_present"] is False
```

Run: `.venv/bin/python -m pytest tests/test_bench_summary.py -v -k "arch_present"`
Expected: FAIL — no `arch_present` key.

- [ ] **Step 2: Extend `_extract_run` and `summarize`**

In `scripts/bench_summary.py`, update the import block:

```python
from bitgn_contest_agent.trace_schema import (
    TraceArch, TraceMeta, TraceOutcome, TraceStep, load_jsonl,
)
```

Modify `_extract_run` to track arch presence. Change signature:

```python
def _extract_run(path: Path) -> tuple[str, float, int, TraceMeta, TraceOutcome, list[int], list[str], int, bool] | None:
    meta: TraceMeta | None = None
    outcome: TraceOutcome | None = None
    divergence_steps: list[int] = []
    step_texts: list[str] = []
    step_wall_ms_sum: int = 0
    arch_present: bool = False
    try:
        for rec in load_jsonl(path):
            if isinstance(rec, TraceMeta):
                meta = rec
            elif isinstance(rec, TraceStep):
                # ... existing block unchanged ...
            elif isinstance(rec, TraceArch):
                arch_present = True
            elif isinstance(rec, TraceOutcome):
                outcome = rec
    except (ValueError, json.JSONDecodeError):
        return None
    if meta is None or outcome is None:
        return None
    score = float(outcome.score) if outcome.score is not None else (
        1.0 if (outcome.reported == "OUTCOME_OK" and outcome.terminated_by == "report_completion") else 0.0
    )
    return (meta.task_id, score, outcome.total_steps, meta, outcome,
            divergence_steps, step_texts, step_wall_ms_sum, arch_present)
```

Update `summarize` callers. In the `for path in _iter_jsonl_files(logs_dir):` loop:
```python
task_id, score, steps, meta, outcome, divergence_steps, step_texts, step_wall_ms_sum, arch_present = run
by_task[task_id].append(
    (score, steps, meta, outcome, divergence_steps, step_texts, step_wall_ms_sum, arch_present)
)
```

Find where per-task dict is built (in `summarize`). After computing other fields, add:
```python
tasks_out[task_id]["arch_present"] = any(e[7] for e in entries)
```

Also update the tuple type hint for `by_task` (it is now 8-tuple).

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest tests/test_bench_summary.py -v`
Expected: PASS (including the new `arch_present` test).

- [ ] **Step 4: Commit**

```bash
git add scripts/bench_summary.py tests/test_bench_summary.py
git commit -m "feat(arch-log): bench_summary reports arch_present per task"
```

---

## Task 9: `scripts/arch_report.py` — new analyser tool

**Files:**
- Create: `scripts/arch_report.py`
- Test: `tests/test_arch_report.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

Run: `.venv/bin/python -m pytest tests/test_arch_report.py -v`
Expected: FAIL — script does not exist.

- [ ] **Step 2: Implement the script**

```python
#!/usr/bin/env python3
"""arch_report — print architecture decision timelines from trace JSONL.

Usage:
    arch_report.py <jsonl>                          one task timeline
    arch_report.py <run-dir>                        all tasks in dir
    arch_report.py <run-dir> --task t100            single task
    arch_report.py <run-dir> --category VALIDATOR_T2
    arch_report.py <run-dir> --category VALIDATOR_T2 --trigger first_transition

Enums come from bitgn_contest_agent.arch_constants — renaming a member
propagates here for free via argparse's `choices=list(Enum)`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

# repo root on path so this script runs from the checkout
_here = Path(__file__).resolve()
_repo_root = _here.parent.parent
if str(_repo_root / "src") not in sys.path:
    sys.path.insert(0, str(_repo_root / "src"))

from bitgn_contest_agent.arch_constants import (  # noqa: E402
    ArchCategory,
    ArchResult,
    RouterSource,
    ValidatorT1Rule,
    ValidatorT2Trigger,
)
from bitgn_contest_agent.trace_schema import (  # noqa: E402
    TraceArch,
    TraceMeta,
    load_jsonl,
)


def _iter_trace_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from sorted(path.rglob("*.jsonl"))
    else:
        raise FileNotFoundError(path)


def _format_row(trace_name: str, rec: TraceArch) -> str:
    step = "-" if rec.at_step is None else str(rec.at_step)
    parts: list[str] = [f"{trace_name:<30}", f"step={step:<4}",
                        f"{rec.category.value:<20}"]
    for key in ("rule", "trigger", "result", "skill", "source"):
        val = getattr(rec, key)
        if val is not None:
            parts.append(f"{key}={val.value if hasattr(val, 'value') else val}")
    if rec.confidence is not None:
        parts.append(f"conf={rec.confidence:.2f}")
    if rec.details:
        parts.append(f"details={rec.details[:80]}")
    return " ".join(parts)


def _matches(rec: TraceArch, args: argparse.Namespace) -> bool:
    if args.category is not None and rec.category != args.category:
        return False
    if args.rule is not None and rec.rule != args.rule:
        return False
    if args.trigger is not None and rec.trigger != args.trigger:
        return False
    if args.result is not None and rec.result != args.result:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arch_report",
        description="Print arch observability timeline from trace JSONL(s)",
    )
    parser.add_argument("path", help="JSONL file or run directory")
    parser.add_argument("--task", default=None, help="task_id filter")
    parser.add_argument(
        "--category", type=ArchCategory, choices=list(ArchCategory),
        default=None,
    )
    parser.add_argument(
        "--rule", type=ValidatorT1Rule, choices=list(ValidatorT1Rule),
        default=None,
    )
    parser.add_argument(
        "--trigger", type=ValidatorT2Trigger, choices=list(ValidatorT2Trigger),
        default=None,
    )
    parser.add_argument(
        "--result", type=ArchResult, choices=list(ArchResult),
        default=None,
    )
    args = parser.parse_args(argv)

    for p in _iter_trace_files(Path(args.path)):
        meta: TraceMeta | None = None
        records: list[TraceArch] = []
        try:
            for rec in load_jsonl(p):
                if isinstance(rec, TraceMeta):
                    meta = rec
                elif isinstance(rec, TraceArch):
                    records.append(rec)
        except (ValueError, Exception) as exc:
            print(f"# skip {p.name}: {exc}", file=sys.stderr)
            continue
        if meta is None:
            continue
        if args.task is not None and meta.task_id != args.task:
            continue
        # Header line per trace with intent preview
        if meta.intent_head:
            print(f"# {p.name}  task={meta.task_id}  intent={meta.intent_head[:100]!r}")
        else:
            print(f"# {p.name}  task={meta.task_id}")
        for rec in records:
            if _matches(rec, args):
                print(_format_row(p.name, rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Make executable:
```bash
chmod +x scripts/arch_report.py
```

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest tests/test_arch_report.py -v`
Expected: PASS (6 tests)

- [ ] **Step 4: Commit**

```bash
git add scripts/arch_report.py tests/test_arch_report.py
git commit -m "feat(arch-log): arch_report.py analyser for per-task timelines"
```

---

## Task 10: Full suite + smoke sanity

**Files:** (none modified — verification only)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest -x`
Expected: PASS (all pre-existing + new tests).

- [ ] **Step 2: Run one local smoke task**

Run the local smoke-test agent loop end-to-end to confirm per-task `.log` and `.jsonl` both populate. If `make bench-smoke` is defined or `bitgn-agent run-benchmark --smoke` works in the local env, use it; otherwise run a single task:

```bash
source .worktrees/plan-b/.env 2>/dev/null || true
.venv/bin/bitgn-agent run-task --task-id t001
```

Expected: in `{log_dir}/{run_id}/` there is both `t001__run0.jsonl` (containing arch records) and `t001__run0.log` (containing `[ARCH:*]` lines with `task=t001 run=0 ...` prefix).

Verify:
```bash
grep "\[ARCH:" "$(ls -t artifacts/**/*.log | head -1)" | head -5
```
Expected: log lines with `task=t001 skill=... category=...` prefixes.

- [ ] **Step 3: Run `arch_report.py` on the smoke trace**

```bash
.venv/bin/python scripts/arch_report.py "$(ls -t artifacts/**/*.jsonl | head -1)"
```

Expected: timeline prints one row per arch record.

- [ ] **Step 4: Commit bump VERSION**

```bash
echo 0.1.9 > VERSION
git add VERSION
git commit -m "chore(release): 0.1.9 — arch observability logging"
```
