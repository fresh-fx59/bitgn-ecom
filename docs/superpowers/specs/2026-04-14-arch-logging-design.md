# Architecture Observability Logging — Design

**Status:** approved 2026-04-14, heading into implementation

**Problem.** When a PROD benchmark run fails we cannot tell whether the
router, validator, or reactive layer fired, what decisions they made,
or why. `[ARCH:*]` log lines exist but go only to stderr, which is not
captured by default. We need automatic, structured, per-task logging
with enough identifiers to cross-reference logs with JSONL traces and
to group tasks across runs by what they actually are.

## Goals

1. Every benchmark run automatically captures architecture decisions
   with zero extra CLI flags.
2. Each log line is self-identifying (task, run, skill, category,
   trace filename) so grep across runs is productive.
3. The same enums drive logic branches, JSONL schema validation, log
   text, and analyser CLI filtering — a rename in one place propagates
   everywhere.
4. Debug workflow for "did the router fire on t100?" is one grep.

## Non-goals

- Log rotation / compression.
- Uploading logs to external aggregators.
- Back-filling historical runs.
- Adding new log sites. We capture what already exists.

## Components

### 1. `arch_constants.py` — enum source of truth

Python 3.11+ `StrEnum`s. Each member's value is the wire string, so
every existing log line's text is byte-identical; the only change is
that call sites reference enum members instead of string literals.

```python
class ArchCategory(StrEnum):
    SKILL_ROUTER      = "SKILL_ROUTER"
    REACTIVE          = "REACTIVE"
    VALIDATOR_T1      = "VALIDATOR_T1"
    VALIDATOR_T2      = "VALIDATOR_T2"
    TERMINAL          = "TERMINAL"
    TERMINAL_R4       = "TERMINAL_R4"
    LOOP_NUDGE        = "LOOP_NUDGE"
    FORMAT_VALIDATOR  = "FORMAT_VALIDATOR"
    BODY_PRESERVATION = "BODY_PRESERVATION"
    TASK_START        = "TASK_START"

class ValidatorT1Rule(StrEnum):
    CONTRADICTION_OK_NEG   = "contradiction_ok_neg"
    CONTRADICTION_CLAR_POS = "contradiction_clar_pos"
    DANGEROUS_DENIED_TO_OK = "dangerous_denied_to_ok"
    MUTATION_GUARD         = "mutation_guard"

class ValidatorT2Trigger(StrEnum):
    FIRST_TRANSITION      = "first_transition"
    CLARIFICATION         = "clarification"
    INBOX_READ            = "inbox_read"
    PROGRESS_CHECK        = "progress_check"
    ENTITY_FINANCE_SEARCH = "entity_finance_search"

class ArchResult(StrEnum):
    OK         = "OK"
    CORRECTED  = "CORRECTED"
    ACCEPT     = "ACCEPT"
    REJECT     = "REJECT"
    MISMATCH   = "MISMATCH"
    CONSISTENT = "CONSISTENT"

class RouterSource(StrEnum):
    TIER1_REGEX = "tier1_regex"
    TIER2_LLM   = "tier2_llm"
    NONE        = "none"
```

Skill category values (FINANCE_LOOKUP, BILL_QUERY, etc.) remain data
in skill markdown. `Router.load` validates uniqueness at startup and
emits one registration log line listing every known category.

### 2. `TraceArch` JSONL record

New additive record kind in `trace_schema.py`:

```python
class TraceArch(_BaseRecord):
    kind: Literal["arch"] = "arch"
    at_step: Optional[int] = None       # None = pre-task (router)
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
    emitted_at: Optional[str] = None    # ISO-8601 UTC, ms precision
```

`TraceMeta` gains `intent_head: Optional[str]` (first 240 chars of
task text) for quick visual indexing. No hash field — hashing is
lexical and rejected for grouping purposes.

Added to `_KIND_TO_MODEL` and the `TraceRecord` union. `extra="ignore"`
keeps old traces parseable and reserves space for future additive
fields.

### 3. `emit_arch` — single emit helper

```python
# src/bitgn_contest_agent/arch_log.py
def emit_arch(writer: Optional[TraceWriter], *,
              category: ArchCategory,
              at_step: Optional[int] = None,
              **fields) -> None:
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

`_format_arch_line` renders `[ARCH:CATEGORY] key=val ...` from the
record itself, so the stderr line and the JSONL record cannot drift.

Every existing `_LOG.info("[ARCH:...] ...")` call site in `agent.py`
and `validator.py` is replaced with an `emit_arch(...)` call. 16
sites; no behavior change in the emitted strings.

### 4. Per-task stderr capture

`logging.FileHandler` installed in `_run_single_task` right after the
`TraceWriter` is created, writing to `trace_path.with_suffix(".log")`.
A filter scopes records to the worker thread via `record.thread ==
worker_tid`, captured at handler-creation time. Handler removed and
closed in `finally`.

This captures everything the structured records do not — backend
retries, rate-limit backoffs, Python tracebacks, any log line we have
not instrumented.

### 5. Self-identifying log lines via `ContextVar`

`logging.basicConfig` installs a `TaskContextFilter` globally in
`main()`. The filter reads a `ContextVar[dict]` and injects these
attributes onto every `LogRecord`:

| Attribute   | Source                                    |
|-------------|-------------------------------------------|
| task_id     | resolved by `harness.start_trial/start_task` |
| run_index   | iteration index                           |
| trace_name  | JSONL filename (`t100__run0.jsonl`)       |
| skill       | router decision (`-` until router fires)  |
| category    | router decision (`UNKNOWN` if no match)   |

`_run_single_task` sets the ContextVar at entry, updates `skill` and
`category` immediately after the router logs its decision, and resets
on exit via the token pattern.

Log format:
```
%(asctime)s %(levelname)s task=%(task_id)s run=%(run_index)s skill=%(skill)s category=%(category)s trace=%(trace_name)s %(name)s %(message)s
```

First line per task is `[TASK_START]`, emitted by `emit_arch` with
`category=ArchCategory.TASK_START` and `details=` containing the
truncated instruction. Full text lives in the `TraceTask` record.

### 6. Analyser updates

**`scripts/bench_summary.py`** — one additive branch counting
`isinstance(rec, TraceArch)` and setting `arch_present: bool` per
task in the summary. No aggregated counts (vanity).

**`scripts/arch_report.py`** — new ad-hoc tool:

```
arch_report.py <jsonl>                  # single task timeline
arch_report.py <run-dir>                # all tasks
arch_report.py <run-dir> --task t100    # single task across iterations
arch_report.py <run-dir> --category SKILL_ROUTER
arch_report.py <run-dir> --category VALIDATOR_T2 --trigger entity_finance_search
```

Arg parsing uses the enums directly: `type=ArchCategory, choices=list(ArchCategory)`. Invalid values fail at argparse time.

Output is aligned one-line-per-event, newest-last:
```
t100__run0  step=-  SKILL_ROUTER  skill=-            source=none       conf=-
t100__run0  step=3  VALIDATOR_T1  rule=mutation_guard
t100__run0  step=11 TERMINAL      verdict=ACCEPT outcome=OUTCOME_OK
```

## Data flow

```
worker thread
  ├─ _run_single_task
  │   ├─ _task_ctx.set({task_id, run_index, trace_name, skill="-", category="-"})
  │   ├─ attach FileHandler(<trace>.log) with thread filter
  │   ├─ TraceWriter.write_meta(...)
  │   ├─ emit_arch(TASK_START, details=instruction[:240])
  │   ├─ agent loop
  │   │     emit_arch(SKILL_ROUTER, skill, source, confidence)
  │   │         └─ then: _task_ctx.get().update(skill, category)
  │   │     emit_arch(VALIDATOR_T1, rule=...)   # every step
  │   │     emit_arch(VALIDATOR_T2, trigger, result)
  │   │     emit_arch(TERMINAL, verdict, reasons)
  │   │     emit_arch(TERMINAL_R4, result, confidence)
  │   ├─ detach/close FileHandler
  │   └─ _task_ctx.reset(token)
```

Every `emit_arch` writes exactly two places: the task's JSONL trace
(via `TraceWriter.append_arch`) and the task's stderr (via the root
logger, which is teed to `<trace>.log` by the per-task FileHandler).

## Testing

| Level       | Test                                                        |
|-------------|-------------------------------------------------------------|
| schema      | `TraceArch` round-trip with enum fields                     |
| schema      | old trace (no arch records) parses and summarises           |
| emit        | `emit_arch` writes record AND stderr; strings match         |
| formatter   | every `ArchCategory` produces `[ARCH:X] k=v` with all fields |
| context     | ContextVar filter injects task/skill/category correctly      |
| context     | unset ContextVar → fields default to `-`                     |
| file tee    | two concurrent tasks → no cross-contamination in `.log`     |
| analyser    | `arch_report.py` filter by `--category VALIDATOR_T2`        |
| analyser    | `arch_report.py` invalid enum value fails at argparse       |
| integration | one PAC1 smoke task → `.log` + `.jsonl` both populated      |

## Migration

Pure additive. Old traces parse unchanged. New traces gain `arch`
records and richer `meta.intent_head`. Existing grep/parse workflows
on stderr keep working — the log-line format changes are prefix
additions (`task=... run=...`) that appear before the existing
payload, and the `[ARCH:*]` payloads themselves are unchanged.

## What this does NOT include

- Typed per-category wrapper methods. Single `emit_arch(**kw)` suffices.
- Aggregate arch counts in bench summary (use `arch_report.py`).
- Hashing task text (lexical ≠ semantic — rejected).
- Uploading logs anywhere.
