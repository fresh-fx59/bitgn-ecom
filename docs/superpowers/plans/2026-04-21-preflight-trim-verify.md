# Preflight Trim + Verification Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove dead preflight-matcher code (9 files, ~1800 LoC) and add a narrow pre-completion verification trigger with three reason codes that target the four currently-failing PROD tasks (t026, t030, t055, t072) without regressing the 100 passing ones.

**Architecture:** Delete the `routed_preflight` pipeline and all five per-skill preflight modules (they never produce `match_found=True` on PROD per log evidence). Keep `prepass` / `preflight_schema` (workflow-rulebook pre-read — real accelerator). Add `verify.py` with a `should_verify()` trigger that fires a single extra LLM round-trip before `report_completion` is accepted, covering `MISSING_REF` (t026), `NUMERIC_MULTIREF` (t030/t055), and `INBOX_GIVEUP` (t072). Retag remaining preflight-style hints as "guesses, not facts."

**Tech Stack:** Python 3.11+, Pydantic v2, pytest. Existing `agent.py` step loop, `session.py`, `trace_writer.py`, `schemas.py`, `prompts.py`.

**Spec:** `docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md`

**Branch:** `feat/preflight-trim-verify` (baseline `main @ 308b676`, 100/104 PROD pass).

---

## File Structure

### Files created

| Path | Responsibility |
|---|---|
| `src/bitgn_contest_agent/verify.py` | `VerifyReason` enum, `WriteOp` dataclass, `classify_answer_shape`, `should_verify`, `build_verification_message`. Pure Python, no LLM calls. ~200 lines. |
| `tests/test_verify_classify.py` | Unit tests for `classify_answer_shape`. |
| `tests/test_verify_trigger.py` | Unit tests for `should_verify` per-reason matrix. |
| `tests/test_verify_message.py` | Unit tests for `build_verification_message` one-reason and multi-reason shapes. |
| `tests/integration/test_agent_verify_missing_ref.py` | Integration: `MISSING_REF` end-to-end. |
| `tests/integration/test_agent_verify_numeric.py` | Integration: `NUMERIC_MULTIREF` end-to-end. |
| `tests/integration/test_agent_verify_inbox_giveup.py` | Integration: `INBOX_GIVEUP` end-to-end. |
| `tests/integration/test_agent_verify_multiple_reasons.py` | Integration: multiple reasons, single round-trip. |
| `tests/integration/test_agent_no_routed_preflight.py` | Integration: agent boots without deleted modules. |
| `tests/integration/test_verify_no_infinite_loop.py` | Integration: at most one verification round per task. |

### Files deleted

| Path | Reason |
|---|---|
| `src/bitgn_contest_agent/routed_preflight.py` | Dispatcher for per-skill preflight; never fires match_found=True on PROD. |
| `src/bitgn_contest_agent/preflight/inbox.py` | Matcher — dead. |
| `src/bitgn_contest_agent/preflight/finance.py` | Matcher — dead. |
| `src/bitgn_contest_agent/preflight/entity.py` | Matcher — dead. |
| `src/bitgn_contest_agent/preflight/project.py` | Matcher — dead. |
| `src/bitgn_contest_agent/preflight/doc_migration.py` | Matcher — dead. |
| `src/bitgn_contest_agent/preflight/unknown.py` | LLM preflight probe — dead. |
| `src/bitgn_contest_agent/preflight/canonicalize.py` | Helper used only by deleted matchers. |
| `src/bitgn_contest_agent/preflight/response.py` | Response model used only by deleted matchers. |
| `tests/preflight/test_inbox.py` | Tests deleted module. |
| `tests/preflight/test_entity.py` | Tests deleted module. |
| `tests/preflight/test_finance.py` | Tests deleted module (if present). |
| `tests/preflight/test_project.py` | Tests deleted module (if present). |
| `tests/preflight/test_doc_migration.py` | Tests deleted module (if present). |
| `tests/preflight/test_unknown.py` | Tests deleted module (if present). |
| `tests/preflight/test_canonicalize.py` | Tests deleted module (if present). |

### Files modified

| Path | Change |
|---|---|
| `src/bitgn_contest_agent/preflight/__init__.py` | Trim re-exports to just `schema`. |
| `src/bitgn_contest_agent/schemas.py` | Drop `Req_PreflightInbox`, `Req_PreflightFinance`, `Req_PreflightEntity`, `Req_PreflightProject`, `Req_PreflightDocMigration` classes from FunctionUnion tuple. Keep `Req_PreflightSchema`. Same for any Rsp_* classes. |
| `src/bitgn_contest_agent/adapter/pcm.py` | Remove 5 dispatch branches for the deleted Req_Preflight* classes (keep `Req_PreflightSchema` branch). |
| `src/bitgn_contest_agent/agent.py` | Remove `_dispatch_routed_preflight` helper and its call site. Add `write_history` accumulator, `verify_attempts` counter, verification wire-up before terminal submit. Import `verify` module. |
| `src/bitgn_contest_agent/prompts.py` | Delete `PREFLIGHT_PROTOCOL` string and its append to `_STATIC_SYSTEM_PROMPT`. Rephrase any remaining entity-resolution text that says "trust its result". |
| `src/bitgn_contest_agent/skill_loader.py` | Drop `preflight` + `preflight_query_field` fields from `BitgnSkill` dataclass and parse step. |
| `src/bitgn_contest_agent/skills/*.md` | Remove `preflight:` and `preflight_query_field:` lines from frontmatter of all 6 skill files. |
| `src/bitgn_contest_agent/trace_writer.py` | Add `append_verify(reasons, changed)` method. |
| `src/bitgn_contest_agent/trace_schema.py` | Add `TraceVerify` record type. |

---

## Phase A — Delete dead code

Context: current logs show `routed_preflight.match_found=True` fires **0/104** times on PROD; the modules have become dead weight. Delete them together with the adapter dispatch branches, the schemas they parse into, and the skill-frontmatter hooks that reference them.

### Task A1: Remove Req_Preflight* classes from schemas.py

**Files:**
- Modify: `src/bitgn_contest_agent/schemas.py`
- Test: existing `tests/test_schemas.py` (run to confirm no regression)

- [ ] **Step 1: Verify baseline tests pass**

Run: `pytest tests/test_schemas.py -v`
Expected: all pass (captures current schema state pre-deletion).

- [ ] **Step 2: Remove preflight Req_* / Rsp_* classes except Req_PreflightSchema**

Edit `src/bitgn_contest_agent/schemas.py`:
- Delete class definitions for `Req_PreflightInbox`, `Req_PreflightFinance`, `Req_PreflightEntity`, `Req_PreflightProject`, `Req_PreflightDocMigration` (and any matching `Rsp_*`).
- Keep `Req_PreflightSchema` (still used by prepass).
- Remove the deleted names from the `FunctionUnion` tuple and from `REQ_MODELS` constant.
- Remove any imports of the deleted classes from the top of the file.

- [ ] **Step 3: Find and fix orphaned imports project-wide**

Run: `grep -rn "Req_PreflightInbox\|Req_PreflightFinance\|Req_PreflightEntity\|Req_PreflightProject\|Req_PreflightDocMigration" src/ tests/`
Expected after the next steps: no results.

Fix each hit:
- `src/bitgn_contest_agent/routed_preflight.py` — will be deleted in A3, no action needed now.
- `src/bitgn_contest_agent/adapter/pcm.py` — delete the 5 `isinstance(req, Req_PreflightX)` branches in the `dispatch` method.
- Any test file importing these classes — will be deleted in A4.

- [ ] **Step 4: Run schema tests to confirm**

Run: `pytest tests/test_schemas.py -v`
Expected: PASS — remaining schema roundtrip tests still green.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/schemas.py src/bitgn_contest_agent/adapter/pcm.py
git commit -m "refactor: drop per-skill Req_Preflight* classes and adapter branches"
git push origin feat/preflight-trim-verify
```

### Task A2: Delete preflight matcher modules

**Files:**
- Delete: `src/bitgn_contest_agent/preflight/inbox.py`
- Delete: `src/bitgn_contest_agent/preflight/finance.py`
- Delete: `src/bitgn_contest_agent/preflight/entity.py`
- Delete: `src/bitgn_contest_agent/preflight/project.py`
- Delete: `src/bitgn_contest_agent/preflight/doc_migration.py`
- Delete: `src/bitgn_contest_agent/preflight/unknown.py`
- Delete: `src/bitgn_contest_agent/preflight/canonicalize.py`
- Delete: `src/bitgn_contest_agent/preflight/response.py`
- Modify: `src/bitgn_contest_agent/preflight/__init__.py`

- [ ] **Step 1: Find all imports of modules to be deleted**

Run:
```bash
grep -rn "from bitgn_contest_agent.preflight.inbox\|from bitgn_contest_agent.preflight.finance\|from bitgn_contest_agent.preflight.entity\|from bitgn_contest_agent.preflight.project\|from bitgn_contest_agent.preflight.doc_migration\|from bitgn_contest_agent.preflight.unknown\|from bitgn_contest_agent.preflight.canonicalize\|from bitgn_contest_agent.preflight.response" src/ tests/
```

Expected: list includes `routed_preflight.py`, `adapter/pcm.py` (5 lazy imports), and the test files in `tests/preflight/*`. All of these get deleted/modified in tasks A3, A1, and A4.

- [ ] **Step 2: Delete the module files**

```bash
rm src/bitgn_contest_agent/preflight/inbox.py
rm src/bitgn_contest_agent/preflight/finance.py
rm src/bitgn_contest_agent/preflight/entity.py
rm src/bitgn_contest_agent/preflight/project.py
rm src/bitgn_contest_agent/preflight/doc_migration.py
rm src/bitgn_contest_agent/preflight/unknown.py
rm src/bitgn_contest_agent/preflight/canonicalize.py
rm src/bitgn_contest_agent/preflight/response.py
```

- [ ] **Step 3: Prune `preflight/__init__.py` re-exports**

Edit `src/bitgn_contest_agent/preflight/__init__.py` to only re-export symbols from `schema.py`:

```python
"""Preflight package — only the workspace-schema pre-read remains.

The per-skill matcher modules (inbox, finance, entity, project,
doc_migration, unknown) were removed on 2026-04-21 after log evidence
showed match_found=True fires 0/104 times on PROD (see spec
docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md).
"""
from bitgn_contest_agent.preflight.schema import (
    WorkspaceSchema,
    parse_schema_content,
    run_preflight_schema,
)

__all__ = ["WorkspaceSchema", "parse_schema_content", "run_preflight_schema"]
```

Only include names that actually exist in `schema.py` — if `parse_schema_content` or `run_preflight_schema` are not exported from that module today, omit the missing names.

- [ ] **Step 4: Verify schema pre-read still works**

Run: `pytest tests/preflight/test_schema.py -v`
Expected: PASS.

If `tests/preflight/test_schema.py` doesn't exist, run the broader prepass test:
Run: `pytest tests/adapter/test_pcm_prepass.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/preflight/
git commit -m "refactor: delete 8 dead preflight matcher modules"
git push origin feat/preflight-trim-verify
```

### Task A3: Delete routed_preflight.py and its call-site

**Files:**
- Delete: `src/bitgn_contest_agent/routed_preflight.py`
- Modify: `src/bitgn_contest_agent/agent.py` (remove `_dispatch_routed_preflight` method and its call around line 309)

- [ ] **Step 1: Remove the call site in agent.py**

In `src/bitgn_contest_agent/agent.py`, find the block that reads (around lines 304-311):

```python
        # Routed preflight — harness-side dispatch driven by the router
        # decision. When the router picked a skill that has a preflight
        # binding (frontmatter), dispatch it now (after prepass provides
        # the WorkspaceSchema) and inject the canonical-narrowing user
        # message before the first LLM step.
        self._dispatch_routed_preflight(
            decision=decision, prepass=prepass, messages=messages,
        )
```

Delete it.

- [ ] **Step 2: Remove the `_dispatch_routed_preflight` method**

In `src/bitgn_contest_agent/agent.py`, find `def _dispatch_routed_preflight(self, *, decision, prepass, messages)` (starts around line 728) and delete the whole method up to (but not including) the next `def`.

- [ ] **Step 3: Delete routed_preflight.py**

```bash
rm src/bitgn_contest_agent/routed_preflight.py
```

- [ ] **Step 4: Find and fix any remaining references**

Run: `grep -rn "routed_preflight\|dispatch_routed_preflight" src/ tests/`
Expected: no results, or only entries in `trace_writer.py` / `trace_schema.py` referencing a historical `append_prepass(cmd="routed_*")` trace-entry format — those trace-entry formats stay; they're string labels, not imports.

If any Python import of the deleted module remains, remove it.

- [ ] **Step 5: Sanity — agent module still imports cleanly**

Run: `python -c "from bitgn_contest_agent.agent import Agent; print('ok')"`
Expected: `ok` (no ImportError).

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/agent.py src/bitgn_contest_agent/routed_preflight.py
git commit -m "refactor: remove routed_preflight dispatcher and call-site"
git push origin feat/preflight-trim-verify
```

### Task A4: Remove preflight frontmatter from skills + tests

**Files:**
- Modify: `src/bitgn_contest_agent/skills/bill_query.md`
- Modify: `src/bitgn_contest_agent/skills/document_migration.md`
- Modify: `src/bitgn_contest_agent/skills/entity_message_lookup.md`
- Modify: `src/bitgn_contest_agent/skills/finance_lookup.md`
- Modify: `src/bitgn_contest_agent/skills/inbox_processing.md`
- Modify: `src/bitgn_contest_agent/skills/project_involvement.md`
- Modify: `src/bitgn_contest_agent/skill_loader.py`
- Delete: `tests/preflight/test_inbox.py`
- Delete: `tests/preflight/test_entity.py`
- Delete: any other `tests/preflight/test_*.py` except `test_schema.py` (if present)

- [ ] **Step 1: Strip preflight frontmatter from skill markdown files**

For each of the 6 skill `.md` files above, delete the two lines:

```
preflight: preflight_<whatever>
preflight_query_field: <field>
```

Keep all other frontmatter unchanged.

Verify: `grep -l "preflight:" src/bitgn_contest_agent/skills/*.md`
Expected: no output (no matches remain).

- [ ] **Step 2: Drop preflight fields from BitgnSkill dataclass**

In `src/bitgn_contest_agent/skill_loader.py`:

Remove the two lines in the dataclass:

```python
    preflight: Optional[str] = None
    preflight_query_field: str = "query"
```

And remove the parse line (currently around line 64):

```python
    preflight_query_field=parsed.get("preflight_query_field", "query") or "query",
```

And the sibling parse for `preflight`:

```python
    preflight=parsed.get("preflight"),
```

- [ ] **Step 3: Delete matcher-module test files**

```bash
rm -f tests/preflight/test_inbox.py
rm -f tests/preflight/test_entity.py
rm -f tests/preflight/test_finance.py
rm -f tests/preflight/test_project.py
rm -f tests/preflight/test_doc_migration.py
rm -f tests/preflight/test_unknown.py
rm -f tests/preflight/test_canonicalize.py
```

Keep: `tests/preflight/test_schema.py`, `tests/preflight/fixtures/tiny_ws/` (used by schema tests).

- [ ] **Step 4: Run the whole test suite**

Run: `pytest tests/ -q`
Expected: PASS — any test that referenced a deleted matcher should have been deleted above.

If any test fails with `ImportError: cannot import name Req_PreflightX` or `from bitgn_contest_agent.preflight.X`, add that file to the deletion list and re-run.

- [ ] **Step 5: Commit**

```bash
git add src/bitgn_contest_agent/skills/ src/bitgn_contest_agent/skill_loader.py tests/preflight/
git commit -m "refactor: remove preflight frontmatter hooks + matcher tests"
git push origin feat/preflight-trim-verify
```

---

## Phase B — Prompt fix

Retag remaining preflight-style hints so the agent treats them as guesses, not facts. This is where t072's "agent blindly trusted preflight = Jana, correct = Nina" failure gets mitigated at the prompt level.

### Task B1: Strip PREFLIGHT_PROTOCOL from system prompt

**Files:**
- Modify: `src/bitgn_contest_agent/prompts.py`
- Test: `tests/test_prompts.py` (add if missing)

- [ ] **Step 1: Write the failing test**

Create or append to `tests/test_prompts.py`:

```python
from bitgn_contest_agent.prompts import system_prompt


def test_system_prompt_has_no_preflight_protocol_section():
    """The per-skill preflight tools are gone (2026-04-21). The system
    prompt must not advertise them or tell the model to trust their
    output."""
    p = system_prompt()
    assert "## Preflight Shortcuts" not in p
    assert "preflight_inbox" not in p
    assert "preflight_finance" not in p
    assert "preflight_entity" not in p
    assert "preflight_project" not in p
    assert "preflight_doc_migration" not in p


def test_system_prompt_does_not_say_trust_preflight():
    """Any preflight-adjacent guidance that remains must not tell the
    model to trust preflight output unconditionally."""
    p = system_prompt()
    lower = p.lower()
    assert "trust its result" not in lower
    assert "treat as ground truth" not in lower
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompts.py -v`
Expected: FAIL on all three assertions in `test_system_prompt_has_no_preflight_protocol_section` and FAIL on `test_system_prompt_does_not_say_trust_preflight` (current prompt contains "trust its result" in the entity-resolution section).

- [ ] **Step 3: Remove the PREFLIGHT_PROTOCOL block**

In `src/bitgn_contest_agent/prompts.py`:

- Delete the entire `PREFLIGHT_PROTOCOL = """ ... """` string (starts with `PREFLIGHT_PROTOCOL = """`, ends at the closing `"""`).
- Delete the line `_STATIC_SYSTEM_PROMPT = _STATIC_SYSTEM_PROMPT + PREFLIGHT_PROTOCOL`.

- [ ] **Step 4: Soften "trust its result" in entity resolution**

In `src/bitgn_contest_agent/prompts.py`, find the `Entity resolution:` block (around line 202). Replace:

```
  - Use the `preflight_entity` tool to resolve any person, device,
    or system reference — it searches names, aliases, relationship
    fields, and descriptions with automatic disambiguation. Trust
    its result; do not manually re-search cast files.
```

With:

```
  - Resolve any person, device, or system reference by searching the
    `20_entities/` (or workspace-equivalent) cast directly — open the
    entity file, read its aliases / relationship / descriptors, and
    verify the match before acting. Do not treat any upstream hint
    as a verified answer.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_prompts.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bitgn_contest_agent/prompts.py tests/test_prompts.py
git commit -m "prompts: drop preflight protocol, retag entity resolution as direct-read"
git push origin feat/preflight-trim-verify
```

---

## Phase C — Verification trigger

Three TDD commits, each adding one or two reasons plus their wire-up.

### Task C1: Scaffolding — classifier + enum + write_history + trace

**Files:**
- Create: `src/bitgn_contest_agent/verify.py`
- Create: `tests/test_verify_classify.py`
- Modify: `src/bitgn_contest_agent/agent.py` (add `write_history` accumulator; no trigger logic yet)
- Modify: `src/bitgn_contest_agent/trace_writer.py` (add `append_verify`)
- Modify: `src/bitgn_contest_agent/trace_schema.py` (add `TraceVerify`)

- [ ] **Step 1: Write failing classifier tests**

Create `tests/test_verify_classify.py`:

```python
from bitgn_contest_agent.verify import AnswerShape, classify_answer_shape
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion


def _ns(answer: str, outcome: str = "OUTCOME_OK") -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["submit"],
        identity_verified=True,
        observation="ready",
        outcome_leaning=outcome,
        function=ReportTaskCompletion(
            tool="report_completion",
            message=answer,
            grounding_refs=[],
            rulebook_notes="n/a",
            outcome_justification="n/a",
            completed_steps_laconic=["done"],
            outcome=outcome,
        ),
    )


def test_classify_numeric_from_answer():
    shape = classify_answer_shape(_ns("6"), task_text="anything")
    assert shape is AnswerShape.NUMERIC


def test_classify_numeric_negative_and_decimal():
    assert classify_answer_shape(_ns("-12"), "x") is AnswerShape.NUMERIC
    assert classify_answer_shape(_ns("3.14"), "x") is AnswerShape.NUMERIC


def test_classify_numeric_from_task_text_when_answer_is_prose():
    shape = classify_answer_shape(
        _ns("six euros total"),
        task_text="how much did vendor charge. Number only.",
    )
    assert shape is AnswerShape.NUMERIC


def test_classify_date_iso():
    assert classify_answer_shape(_ns("2026-04-21"), "x") is AnswerShape.DATE


def test_classify_date_from_task_hint():
    shape = classify_answer_shape(
        _ns("april 21st"),
        task_text="what was the start date? Date only, YYYY-MM-DD.",
    )
    assert shape is AnswerShape.DATE


def test_classify_none_clarification_shape():
    ns = _ns("need more info", outcome="OUTCOME_NONE_CLARIFICATION")
    assert classify_answer_shape(ns, "x") is AnswerShape.NONE_CLARIFICATION


def test_classify_freeform_default():
    shape = classify_answer_shape(
        _ns("here is a long explanation of the bill context"),
        task_text="describe the vendor relationship",
    )
    assert shape is AnswerShape.FREEFORM
```

- [ ] **Step 2: Run tests — they fail (no `verify` module yet)**

Run: `pytest tests/test_verify_classify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bitgn_contest_agent.verify'`.

- [ ] **Step 3: Create verify.py with enum + classifier**

Create `src/bitgn_contest_agent/verify.py`:

```python
"""Pre-completion verification trigger (v1, 3 reasons).

Spec: docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md

Fires before report_completion is accepted, at most once per task.
All reason detection is deterministic — no LLM calls in this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion


class AnswerShape(str, Enum):
    NUMERIC = "NUMERIC"
    DATE = "DATE"
    PATH_LIST = "PATH_LIST"
    MESSAGE_QUOTE = "MESSAGE_QUOTE"
    ACTION_CONFIRMATION = "ACTION_CONFIRMATION"
    NONE_CLARIFICATION = "NONE_CLARIFICATION"
    FREEFORM = "FREEFORM"


class VerifyReason(str, Enum):
    MISSING_REF = "MISSING_REF"
    NUMERIC_MULTIREF = "NUMERIC_MULTIREF"
    INBOX_GIVEUP = "INBOX_GIVEUP"


@dataclass(frozen=True)
class WriteOp:
    """Record of a single write/delete/move the agent performed."""
    op: str           # "write" | "delete" | "move"
    path: str
    step: int
    content: Optional[str] = None  # None for delete/move


_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY_RE = re.compile(r"^\d{2}[-/]\d{2}[-/]\d{4}$")
_MDY_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

_TASK_NUMBER_ONLY_RE = re.compile(
    r"(?i)\b(answer\s+with\s+(a|the)?\s*number|number\s+only|numeric\s+only)\b"
)
_TASK_DATE_ONLY_RE = re.compile(
    r"(?i)\b(date\s+only|answer\s+(with|in)\s+(a|the)?\s*date|yyyy-mm-dd|date\s+format)\b"
)


def classify_answer_shape(next_step: NextStep, task_text: str) -> AnswerShape:
    """Deterministically classify the answer shape of a completion.

    Precedence:
      1. NONE_CLARIFICATION — outcome says so
      2. NUMERIC — answer matches numeric regex OR task demands a number
      3. DATE — answer matches a date regex OR task demands a date
      4. FREEFORM — otherwise
    """
    fn = next_step.function
    if not isinstance(fn, ReportTaskCompletion):
        return AnswerShape.FREEFORM
    if fn.outcome == "OUTCOME_NONE_CLARIFICATION":
        return AnswerShape.NONE_CLARIFICATION
    answer = (fn.message or "").strip()
    task = task_text or ""
    if _NUMERIC_RE.match(answer) or _TASK_NUMBER_ONLY_RE.search(task):
        return AnswerShape.NUMERIC
    if (_ISO_DATE_RE.match(answer) or _DMY_RE.match(answer)
            or _MDY_RE.match(answer) or _TASK_DATE_ONLY_RE.search(task)):
        return AnswerShape.DATE
    return AnswerShape.FREEFORM


# should_verify and build_verification_message are added in C2/C3.
def should_verify(*args, **kwargs):
    """Placeholder until C2 introduces the real trigger logic."""
    return []


def build_verification_message(*args, **kwargs):
    """Placeholder until C2 introduces the real message builder."""
    raise NotImplementedError
```

- [ ] **Step 4: Run classifier tests — they pass**

Run: `pytest tests/test_verify_classify.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Add write_history and verify_attempts to agent.py**

In `src/bitgn_contest_agent/agent.py`:

Add the import near the top:

```python
from bitgn_contest_agent.verify import VerifyReason, WriteOp
```

In the `run(...)` method, find the line:

```python
        read_cache: dict[str, str] = {}  # path → content at read time
```

Add immediately after it:

```python
        write_history: list[WriteOp] = []  # every successful write/delete/move
        verify_attempts = 0  # hard cap ≤1 per task
```

In the tool-dispatch block, find the spot that records mutations (around line 518-521):

```python
                    mut_path = getattr(fn, "path", "") or getattr(fn, "from_name", "")
                    session.mutations.append((tool_name, mut_path))
```

Append after those two lines (still inside the same `if tool_result.ok:` block):

```python
                    if tool_name in ("write", "delete", "move"):
                        write_history.append(WriteOp(
                            op=tool_name,
                            path=mut_path,
                            step=step_idx,
                            content=getattr(fn, "content", None) if tool_name == "write" else None,
                        ))
```

- [ ] **Step 6: Add TraceVerify record and append_verify**

In `src/bitgn_contest_agent/trace_schema.py`, add a new record model after `TraceEvent`:

```python
class TraceVerify(BaseModel):
    """A pre-completion verification round fired by verify.should_verify."""
    kind: Literal["verify"] = "verify"
    at_step: int
    reasons: list[str]
    changed: bool   # True if the post-verify completion differed
```

In `src/bitgn_contest_agent/trace_writer.py`, add a method next to `append_event`:

```python
    def append_verify(
        self,
        *,
        at_step: int,
        reasons: list[str],
        changed: bool,
    ) -> None:
        rec = TraceVerify(
            at_step=at_step,
            reasons=reasons,
            changed=changed,
        )
        self._write(rec.model_dump(mode="json"))
```

And add the `TraceVerify` import to that file:

```python
from bitgn_contest_agent.trace_schema import (
    ...,
    TraceVerify,
)
```

- [ ] **Step 7: Run broader tests**

Run: `pytest tests/ -q -x --ignore=tests/integration`
Expected: PASS — scaffolding is inert (no trigger runs yet).

- [ ] **Step 8: Commit**

```bash
git add src/bitgn_contest_agent/verify.py src/bitgn_contest_agent/agent.py src/bitgn_contest_agent/trace_schema.py src/bitgn_contest_agent/trace_writer.py tests/test_verify_classify.py
git commit -m "feat(verify): classifier + enum + write_history + trace scaffolding"
git push origin feat/preflight-trim-verify
```

### Task C2: MISSING_REF + NUMERIC_MULTIREF

Both share answer-parsing plumbing. Tests first.

**Files:**
- Modify: `src/bitgn_contest_agent/verify.py` (real `should_verify` + `build_verification_message`)
- Create: `tests/test_verify_trigger.py`
- Create: `tests/test_verify_message.py`
- Create: `tests/integration/test_agent_verify_missing_ref.py`
- Create: `tests/integration/test_agent_verify_numeric.py`
- Modify: `src/bitgn_contest_agent/agent.py` (wire the trigger into the terminal path)

- [ ] **Step 1: Write failing trigger tests**

Create `tests/test_verify_trigger.py`:

```python
from bitgn_contest_agent.verify import (
    VerifyReason, WriteOp, should_verify,
)
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion
from bitgn_contest_agent.session import Session


def _completion(message: str, refs=None, outcome="OUTCOME_OK") -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["submit"],
        identity_verified=True,
        observation="ready",
        outcome_leaning=outcome,
        function=ReportTaskCompletion(
            tool="report_completion",
            message=message,
            grounding_refs=list(refs or []),
            rulebook_notes="n/a",
            outcome_justification="n/a",
            completed_steps_laconic=["done"],
            outcome=outcome,
        ),
    )


# ── MISSING_REF ──────────────────────────────────────────────────────

def test_missing_ref_fires_when_answer_cites_unread_path():
    ns = _completion(
        message="see 40_projects/hearthline/README.md for details",
        refs=["40_projects/hearthline/README.md"],
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},  # never read that path
        write_history=[],
        task_text="when did the project start?",
        skill_name="project-involvement",
    )
    assert VerifyReason.MISSING_REF in reasons


def test_missing_ref_quiet_when_path_was_read():
    ns = _completion(
        message="see 40_projects/hearthline/README.md",
        refs=["40_projects/hearthline/README.md"],
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={"40_projects/hearthline/README.md": "…"},
        write_history=[],
        task_text="when did the project start?",
        skill_name="project-involvement",
    )
    assert VerifyReason.MISSING_REF not in reasons


def test_missing_ref_quiet_on_freeform_no_paths():
    ns = _completion(message="Nothing to cite.")
    reasons = should_verify(
        next_step=ns, session=Session(), read_cache={},
        write_history=[], task_text="describe", skill_name=None,
    )
    assert VerifyReason.MISSING_REF not in reasons


# ── NUMERIC_MULTIREF ─────────────────────────────────────────────────

def test_numeric_multiref_fires_on_scalar_with_many_records():
    ns = _completion(message="12")
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={
            "50_finance/purchases/bill_a.md": "amount: 6",
            "50_finance/purchases/bill_b.md": "amount: 6",
        },
        write_history=[],
        task_text="how much did vendor X charge for relay modules? Number only.",
        skill_name="finance-lookup",
    )
    assert VerifyReason.NUMERIC_MULTIREF in reasons


def test_numeric_multiref_quiet_with_single_record():
    ns = _completion(message="6")
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={"50_finance/purchases/bill_a.md": "amount: 6"},
        write_history=[],
        task_text="how much did vendor X charge? Number only.",
        skill_name="finance-lookup",
    )
    assert VerifyReason.NUMERIC_MULTIREF not in reasons


def test_numeric_multiref_quiet_on_freeform():
    ns = _completion(message="about half the sum")
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={
            "50_finance/purchases/bill_a.md": "amount: 6",
            "50_finance/purchases/bill_b.md": "amount: 6",
        },
        write_history=[],
        task_text="summarize the billing", skill_name=None,
    )
    assert VerifyReason.NUMERIC_MULTIREF not in reasons


# ── priority ordering ───────────────────────────────────────────────

def test_reasons_return_in_priority_order():
    # Contrive a completion that trips both MISSING_REF and NUMERIC_MULTIREF.
    ns = _completion(
        message="12 (see 50_finance/purchases/bill_a.md and bill_b.md)",
        refs=["50_finance/purchases/bill_a.md",
              "50_finance/purchases/bill_b.md"],
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={
            # Only bill_a was read; cited bill_b unread → MISSING_REF
            "50_finance/purchases/bill_a.md": "amount: 6",
        },
        write_history=[],
        task_text="how much did vendor X charge? Number only.",
        skill_name="finance-lookup",
    )
    # Spec §4: MISSING_REF ranks higher than NUMERIC_MULTIREF.
    assert reasons[0] == VerifyReason.MISSING_REF
```

Create `tests/test_verify_message.py`:

```python
from bitgn_contest_agent.verify import (
    VerifyReason, WriteOp, build_verification_message,
)
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion


def _completion(message: str, refs=None) -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["submit"],
        identity_verified=True,
        observation="ready",
        outcome_leaning="OUTCOME_OK",
        function=ReportTaskCompletion(
            tool="report_completion",
            message=message,
            grounding_refs=list(refs or []),
            rulebook_notes="n/a",
            outcome_justification="n/a",
            completed_steps_laconic=["done"],
            outcome="OUTCOME_OK",
        ),
    )


def test_message_has_missing_ref_section_with_gap_list():
    msg = build_verification_message(
        reasons=[VerifyReason.MISSING_REF],
        next_step=_completion(
            "cites 40_projects/hearthline/README.md",
            refs=["40_projects/hearthline/README.md"],
        ),
        read_cache={"00_inbox/foo.md": "x"},
        write_history=[],
        task_text="when did it start?",
    )
    assert "MISSING_REF" in msg
    assert "40_projects/hearthline/README.md" in msg
    assert "Before submitting" in msg or "Before the answer is accepted" in msg


def test_message_has_numeric_multiref_section_with_candidate_paths():
    msg = build_verification_message(
        reasons=[VerifyReason.NUMERIC_MULTIREF],
        next_step=_completion("12"),
        read_cache={
            "50_finance/purchases/bill_a.md": "amount: 6",
            "50_finance/purchases/bill_b.md": "amount: 6",
        },
        write_history=[],
        task_text="Number only.",
    )
    assert "NUMERIC_MULTIREF" in msg
    assert "50_finance/purchases/bill_a.md" in msg
    assert "50_finance/purchases/bill_b.md" in msg


def test_message_combines_multiple_reasons_in_one_message():
    msg = build_verification_message(
        reasons=[VerifyReason.MISSING_REF, VerifyReason.NUMERIC_MULTIREF],
        next_step=_completion(
            "12 (ref 40_projects/hearthline/README.md)",
            refs=["40_projects/hearthline/README.md"],
        ),
        read_cache={
            "50_finance/purchases/bill_a.md": "amount: 6",
            "50_finance/purchases/bill_b.md": "amount: 6",
        },
        write_history=[],
        task_text="Number only.",
    )
    # Both sections present, each with its own heading.
    assert msg.count("## ") >= 2
    assert "MISSING_REF" in msg and "NUMERIC_MULTIREF" in msg
```

- [ ] **Step 2: Run the new tests — they should fail on `should_verify` returning `[]` and `build_verification_message` raising NotImplementedError**

Run: `pytest tests/test_verify_trigger.py tests/test_verify_message.py -v`
Expected: FAIL (many).

- [ ] **Step 3: Implement `should_verify` (C2 scope) + `build_verification_message`**

Replace the placeholder `should_verify` and `build_verification_message` in `src/bitgn_contest_agent/verify.py` with:

```python
# Paths cited in answer text that look like workspace paths.
# Matches e.g. "40_projects/hearthline/README.md" or "50_finance/.../foo.md".
_PATH_RE = re.compile(
    r"\b[0-9]{2}_[a-z_]+/[^\s,;()]+?\.(?:md|MD|yaml|yml|txt)\b"
)


def _paths_cited_in_answer(ns: NextStep) -> list[str]:
    fn = ns.function
    if not isinstance(fn, ReportTaskCompletion):
        return []
    candidates: list[str] = []
    # grounding_refs is authoritative when present.
    candidates.extend(fn.grounding_refs or [])
    # Also harvest path-shaped tokens from the free-text message so the
    # agent can't evade the check by moving references out of refs[].
    candidates.extend(_PATH_RE.findall(fn.message or ""))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _read_cache_has(read_cache: dict[str, str], path: str) -> bool:
    """Normalized membership check.

    Workspace paths may be stored with or without a leading slash; compare
    both side's `.lstrip("/")` form.
    """
    norm = path.lstrip("/")
    return any(k.lstrip("/") == norm for k in read_cache.keys())


def should_verify(
    *,
    next_step: NextStep,
    session,  # bitgn_contest_agent.session.Session — unused in v1, kept for future
    read_cache: dict[str, str],
    write_history: list[WriteOp],
    task_text: str,
    skill_name: Optional[str],
) -> list[VerifyReason]:
    """Return triggered verification reasons, in priority order.

    Priority (spec §4): MISSING_REF > INBOX_GIVEUP > NUMERIC_MULTIREF.
    """
    del session  # reserved for v2; silence linters
    fn = next_step.function
    if not isinstance(fn, ReportTaskCompletion):
        return []

    reasons: list[VerifyReason] = []
    shape = classify_answer_shape(next_step, task_text)

    # MISSING_REF — paths cited but not read.
    cited = _paths_cited_in_answer(next_step)
    missing = [p for p in cited if not _read_cache_has(read_cache, p)]
    if missing:
        reasons.append(VerifyReason.MISSING_REF)

    # NUMERIC_MULTIREF — scalar answer, ≥2 same-shape records read.
    if shape in (AnswerShape.NUMERIC, AnswerShape.DATE):
        if len(read_cache) >= 2:
            reasons.append(VerifyReason.NUMERIC_MULTIREF)

    # INBOX_GIVEUP — added in C3.

    return reasons


def _section_missing_ref(
    next_step: NextStep, read_cache: dict[str, str],
) -> str:
    cited = _paths_cited_in_answer(next_step)
    missing = [p for p in cited if not _read_cache_has(read_cache, p)]
    read_list = "\n  ".join(sorted(read_cache.keys())) or "(nothing)"
    miss_list = "\n  ".join(missing)
    return (
        "## MISSING_REF\n"
        "Your answer cites paths that you did not read this run. "
        "The scorer rejects answers that reference files the agent "
        "never opened.\n\n"
        f"Paths cited in your answer:\n  " + "\n  ".join(cited) + "\n\n"
        f"Paths you read this run:\n  {read_list}\n\n"
        f"Missing (cited but not read):\n  {miss_list}\n\n"
        "Open each missing path before re-emitting report_completion."
    )


def _section_numeric_multiref(
    next_step: NextStep, read_cache: dict[str, str], task_text: str,
) -> str:
    fn = next_step.function
    answer = fn.message if isinstance(fn, ReportTaskCompletion) else ""
    paths = "\n  ".join(sorted(read_cache.keys())) or "(nothing)"
    return (
        "## NUMERIC_MULTIREF\n"
        f"Task: {task_text.strip()[:300]}\n"
        f"Your scalar answer: {answer!r}\n"
        f"You read {len(read_cache)} candidate record(s):\n  {paths}\n\n"
        "Re-derive the answer citing one evidence path per numerical "
        "component (e.g. 'bill_a.md amount=6, bill_b.md amount=6 → 12'). "
        "Confirm every addend belongs to the set the task's filter asks "
        "for (entity, date range, line-item). Re-emit report_completion "
        "with the corrected answer if the derivation changed it, or the "
        "same answer with explicit arithmetic in outcome_justification "
        "if it was already right."
    )


def build_verification_message(
    reasons: list[VerifyReason],
    next_step: NextStep,
    read_cache: dict[str, str],
    write_history: list[WriteOp],
    task_text: str,
) -> str:
    """Produce a single multi-section user message covering every reason.

    Sections are emitted in priority order (same order as
    `should_verify` returned them), each separated by a blank line.
    """
    del write_history  # unused until C3 adds INBOX_GIVEUP
    intro = (
        "Before the answer is accepted, address the following checks. "
        "If the evidence confirms your current answer, you can re-emit "
        "the same report_completion — just make the justification "
        "explicit. If the evidence contradicts it, correct the answer.\n"
    )
    sections: list[str] = []
    for r in reasons:
        if r is VerifyReason.MISSING_REF:
            sections.append(_section_missing_ref(next_step, read_cache))
        elif r is VerifyReason.NUMERIC_MULTIREF:
            sections.append(
                _section_numeric_multiref(next_step, read_cache, task_text)
            )
        # INBOX_GIVEUP — added in C3.
    return intro + "\n" + "\n\n".join(sections)
```

- [ ] **Step 4: Run unit tests — they should pass**

Run: `pytest tests/test_verify_trigger.py tests/test_verify_message.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the trigger into agent.py**

In `src/bitgn_contest_agent/agent.py`, locate the terminal-accept path (starts around line 408, `if isinstance(fn, ReportTaskCompletion):`). Modify the `if verdict.ok:` branch so that before `submit_terminal`, we give the verify trigger a chance:

Before this current code:

```python
            if isinstance(fn, ReportTaskCompletion):
                verdict = self._validator.check_terminal(session, step_obj)
                if verdict.ok:
                    tool_result = self._adapter.submit_terminal(fn)
                    enforcer_action = "accept"
```

Replace the `if verdict.ok:` block with:

```python
            if isinstance(fn, ReportTaskCompletion):
                verdict = self._validator.check_terminal(session, step_obj)
                if verdict.ok:
                    # Pre-completion verification (spec 2026-04-21).
                    # Hard cap: 1 verification round per task.
                    if verify_attempts == 0:
                        from bitgn_contest_agent.verify import (
                            build_verification_message as _bv,
                            should_verify as _sv,
                        )
                        v_reasons = _sv(
                            next_step=step_obj,
                            session=session,
                            read_cache=read_cache,
                            write_history=write_history,
                            task_text=task_text,
                            skill_name=(decision.skill_name if decision else None),
                        )
                    else:
                        v_reasons = []
                    if v_reasons:
                        verify_attempts += 1
                        verify_messages = list(messages) + [
                            Message(
                                role="assistant",
                                content=step_obj.model_dump_json(),
                            ),
                            Message(
                                role="user",
                                content=_bv(
                                    reasons=v_reasons,
                                    next_step=step_obj,
                                    read_cache=read_cache,
                                    write_history=write_history,
                                    task_text=task_text,
                                ),
                            ),
                        ]
                        try:
                            verify_result = self._call_backend_with_retry(
                                verify_messages, at_step=step_idx,
                            )
                        except ValidationError:
                            verify_result = None
                        changed = False
                        if verify_result is not None:
                            totals.prompt_tokens += verify_result.prompt_tokens
                            totals.completion_tokens += verify_result.completion_tokens
                            totals.reasoning_tokens += verify_result.reasoning_tokens
                            totals.llm_calls += 1
                            v_step = verify_result.parsed
                            v_fn = v_step.function
                            if isinstance(v_fn, ReportTaskCompletion):
                                if v_fn.model_dump() != fn.model_dump():
                                    changed = True
                                    step_obj = v_step
                                    fn = v_fn
                        self._writer.append_verify(
                            at_step=step_idx,
                            reasons=[r.value for r in v_reasons],
                            changed=changed,
                        )
                    tool_result = self._adapter.submit_terminal(fn)
                    enforcer_action = "accept"
```

Leave the `else:` branch (enforcer reject → retry) unchanged.

- [ ] **Step 6: Write integration test for MISSING_REF**

Create `tests/integration/test_agent_verify_missing_ref.py`:

```python
"""Integration: agent cites an unread path → verification round fires,
model re-emits after reading."""
from __future__ import annotations

import json

from tests.integration.agent_harness import (  # see note below if missing
    run_agent_with_mock_backend,
)


def test_agent_fires_missing_ref_and_records_trace():
    """If the first completion cites a path not in read_cache, verify
    injects a MISSING_REF message and records a trace event."""
    calls: list[dict] = []

    def backend(messages, **_):
        # Call 1: report_completion citing an unread path.
        # Call 2: (after MISSING_REF nudge) report_completion with no
        # unread-path citation so the second verify returns [].
        calls.append({"role": "call", "n": len(calls) + 1})
        if len(calls) == 1:
            return {
                "current_state": "done",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "cited",
                "outcome_leaning": "OUTCOME_OK",
                "function": {
                    "tool": "report_completion",
                    "message": "see 40_projects/foo/README.md for detail",
                    "grounding_refs": ["40_projects/foo/README.md"],
                    "rulebook_notes": "none",
                    "outcome_justification": "cited",
                    "completed_steps_laconic": ["done"],
                    "outcome": "OUTCOME_OK",
                },
            }
        return {
            "current_state": "done",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "cited",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "answer refined",
                "grounding_refs": [],
                "rulebook_notes": "none",
                "outcome_justification": "refined",
                "completed_steps_laconic": ["done"],
                "outcome": "OUTCOME_OK",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-01",
        task_text="when did project X start?",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1
    assert "MISSING_REF" in verify_events[0]["reasons"]
    assert verify_events[0]["changed"] is True
```

If `tests/integration/agent_harness.py` with `run_agent_with_mock_backend` does not exist, a minimal harness is defined next.

- [ ] **Step 7: Create the integration harness if needed**

Check:

Run: `ls tests/integration/agent_harness.py`

If missing, create `tests/integration/agent_harness.py` with the smallest runnable harness:

```python
"""Minimal mock-backend harness for verify integration tests.

Runs the real `Agent.run(...)` loop with a pytest-friendly stub for the
backend, adapter, router, validator, and trace writer. Returns the list
of trace records the run would have written.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.agent import Agent
from bitgn_contest_agent.backend.base import NextStepResult
from bitgn_contest_agent.schemas import NextStep
from bitgn_contest_agent.validator import Verdict


class _Writer:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append_task(self, **kw): self.records.append({"kind": "task", **kw})
    def append_step(self, **kw): self.records.append({"kind": "step", **kw})
    def append_event(self, **kw): self.records.append({"kind": "event", **kw})
    def append_pcm_op(self, **kw): pass
    def append_prepass(self, **kw): pass
    def append_verify(self, **kw): self.records.append({"kind": "verify", **kw})
    def close(self): pass


class _Adapter:
    def run_prepass(self, *, session, trace_writer):
        @dataclass
        class _Prepass:
            bootstrap_content: list[str] = ()
            schema: Any = None
        return _Prepass(bootstrap_content=[])

    def submit_terminal(self, fn) -> ToolResult:
        return ToolResult(ok=True, content="accepted", refs=(), wall_ms=1)


class _Validator:
    def check_terminal(self, session, step_obj):
        return Verdict(ok=True, reasons=[])


class _Backend:
    def __init__(self, handler: Callable[..., dict]) -> None:
        self.handler = handler

    def next_step(self, messages, **kw) -> NextStepResult:
        raw = self.handler(messages, **kw)
        parsed = NextStep.model_validate(raw)
        return NextStepResult(
            parsed=parsed,
            raw_json=parsed.model_dump_json(),
            prompt_tokens=10, completion_tokens=5, reasoning_tokens=0,
        )


def run_agent_with_mock_backend(
    *, task_id: str, task_text: str,
    backend: Callable[..., dict],
) -> list[dict]:
    writer = _Writer()
    agent = Agent(
        backend=_Backend(backend),
        adapter=_Adapter(),
        validator=_Validator(),
        router=None,
        writer=writer,
        max_steps=5,
    )
    agent.run(task_id=task_id, task_text=task_text)
    return writer.records
```

Note: If `Agent.__init__` uses different kwarg names in this repo, adjust the `Agent(...)` construction above to match. If `Agent` does not accept a writer kwarg directly, inspect `agent.py` and pass the actual construction kwargs — the harness exists to exercise the terminal path and read trace events, nothing more.

- [ ] **Step 8: Run the MISSING_REF integration test**

Run: `pytest tests/integration/test_agent_verify_missing_ref.py -v`
Expected: PASS — one verify event with `MISSING_REF` in `reasons` and `changed == True`.

- [ ] **Step 9: Write the NUMERIC_MULTIREF integration test**

Create `tests/integration/test_agent_verify_numeric.py`:

```python
"""Integration: numeric answer + multiple candidate reads → NUMERIC_MULTIREF."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_agent_fires_numeric_multiref_with_two_bills():
    """Agent reads 2 bills and returns a numeric answer → verify fires."""
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        n = len(calls)
        if n == 1:
            return {
                "current_state": "reading first bill",
                "plan_remaining_steps_brief": ["read_another", "total"],
                "identity_verified": True,
                "observation": "new bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {
                    "tool": "read",
                    "path": "50_finance/purchases/bill_a.md",
                },
            }
        if n == 2:
            return {
                "current_state": "reading second bill",
                "plan_remaining_steps_brief": ["total"],
                "identity_verified": True,
                "observation": "new bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {
                    "tool": "read",
                    "path": "50_finance/purchases/bill_b.md",
                },
            }
        # Call 3: scalar completion on 2 candidates → verify should fire.
        if n == 3:
            return {
                "current_state": "totaled",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "have the number",
                "outcome_leaning": "OUTCOME_OK",
                "function": {
                    "tool": "report_completion",
                    "message": "12",
                    "grounding_refs": [
                        "50_finance/purchases/bill_a.md",
                        "50_finance/purchases/bill_b.md",
                    ],
                    "rulebook_notes": "none",
                    "outcome_justification": "summed",
                    "completed_steps_laconic": ["read", "sum"],
                    "outcome": "OUTCOME_OK",
                },
            }
        # Call 4: re-emit after verification nudge (same or different answer).
        return {
            "current_state": "verified",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "re-derived",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "6",
                "grounding_refs": [
                    "50_finance/purchases/bill_a.md",
                ],
                "rulebook_notes": "none",
                "outcome_justification": "one bill matched the filter",
                "completed_steps_laconic": ["re-derived"],
                "outcome": "OUTCOME_OK",
            },
        }

    # NB: _Adapter in the harness doesn't return real read content; if
    # the agent's cache key is the read path (even with empty content),
    # the trigger's len(read_cache) >= 2 still fires. If the real agent
    # only caches on non-empty content, adjust the harness's fake tool
    # dispatcher accordingly.
    trace = run_agent_with_mock_backend(
        task_id="t-verify-02",
        task_text="how much did vendor X charge in total? Number only.",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1
    assert "NUMERIC_MULTIREF" in verify_events[0]["reasons"]
```

- [ ] **Step 10: If the harness's mock tool dispatch doesn't populate read_cache, extend it now**

The harness in step 7 fakes only the backend; the real agent loop calls `self._adapter.dispatch(fn)` for `read`/`write` tools, which feeds `read_cache`. If `run_agent_with_mock_backend` doesn't already have a mock adapter with a `dispatch` method returning a `ToolResult` for `read` ops, extend `_Adapter` in `tests/integration/agent_harness.py`:

```python
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
            return ToolResult(ok=True, content=self.reads[path], refs=(), wall_ms=1)
        return ToolResult(ok=True, content="", refs=(), wall_ms=1)

    def submit_terminal(self, fn) -> ToolResult:
        return ToolResult(ok=True, content="accepted", refs=(), wall_ms=1)
```

- [ ] **Step 11: Run the NUMERIC_MULTIREF integration test**

Run: `pytest tests/integration/test_agent_verify_numeric.py -v`
Expected: PASS.

- [ ] **Step 12: Run the whole suite**

Run: `pytest tests/ -q`
Expected: PASS — no regressions outside the verify tests.

- [ ] **Step 13: Commit**

```bash
git add src/bitgn_contest_agent/verify.py src/bitgn_contest_agent/agent.py tests/test_verify_trigger.py tests/test_verify_message.py tests/integration/agent_harness.py tests/integration/test_agent_verify_missing_ref.py tests/integration/test_agent_verify_numeric.py
git commit -m "feat(verify): MISSING_REF + NUMERIC_MULTIREF trigger + message"
git push origin feat/preflight-trim-verify
```

### Task C3: INBOX_GIVEUP + multi-reason test + cap test

**Files:**
- Modify: `src/bitgn_contest_agent/verify.py` (extend `should_verify` + `build_verification_message`)
- Modify: `tests/test_verify_trigger.py` (add INBOX_GIVEUP tests)
- Modify: `tests/test_verify_message.py` (add INBOX_GIVEUP section test)
- Create: `tests/integration/test_agent_verify_inbox_giveup.py`
- Create: `tests/integration/test_agent_verify_multiple_reasons.py`
- Create: `tests/integration/test_verify_no_infinite_loop.py`
- Create: `tests/integration/test_agent_no_routed_preflight.py`

- [ ] **Step 1: Extend test_verify_trigger.py with INBOX_GIVEUP cases**

Append to `tests/test_verify_trigger.py`:

```python
def test_inbox_giveup_fires_on_none_clarification_without_write():
    ns = _completion(
        message="I need more info.",
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[],
        task_text="take care of the next message in inbox",
        skill_name="inbox-processing",
    )
    assert VerifyReason.INBOX_GIVEUP in reasons


def test_inbox_giveup_quiet_when_outbox_write_exists():
    from bitgn_contest_agent.verify import WriteOp
    ns = _completion(
        message="I need more info.",
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[
            WriteOp(op="write", path="60_outbox/outbox/eml_x.md",
                    step=2, content="reply body"),
        ],
        task_text="take care of the next message in inbox",
        skill_name="inbox-processing",
    )
    assert VerifyReason.INBOX_GIVEUP not in reasons


def test_inbox_giveup_quiet_on_non_inbox_skill():
    ns = _completion(
        message="I need more info.",
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[],
        task_text="how much did vendor X charge?",
        skill_name="finance-lookup",
    )
    assert VerifyReason.INBOX_GIVEUP not in reasons


def test_inbox_giveup_quiet_on_ok_outcome():
    ns = _completion(
        message="done",
        outcome="OUTCOME_OK",
    )
    reasons = should_verify(
        next_step=ns,
        session=Session(),
        read_cache={},
        write_history=[],
        task_text="take care of the next message in inbox",
        skill_name="inbox-processing",
    )
    assert VerifyReason.INBOX_GIVEUP not in reasons
```

- [ ] **Step 2: Extend test_verify_message.py with INBOX_GIVEUP section**

Append to `tests/test_verify_message.py`:

```python
def test_message_has_inbox_giveup_section():
    ns = NextStep(
        current_state="stuck",
        plan_remaining_steps_brief=["submit"],
        identity_verified=True,
        observation="stuck",
        outcome_leaning="OUTCOME_NONE_CLARIFICATION",
        function=ReportTaskCompletion(
            tool="report_completion",
            message="need more info",
            grounding_refs=[],
            rulebook_notes="n/a",
            outcome_justification="n/a",
            completed_steps_laconic=["done"],
            outcome="OUTCOME_NONE_CLARIFICATION",
        ),
    )
    msg = build_verification_message(
        reasons=[VerifyReason.INBOX_GIVEUP],
        next_step=ns,
        read_cache={},
        write_history=[],
        task_text="take care of the next message in inbox",
    )
    assert "INBOX_GIVEUP" in msg
    assert "sender" in msg.lower() or "alias" in msg.lower()
```

- [ ] **Step 3: Run — failing**

Run: `pytest tests/test_verify_trigger.py tests/test_verify_message.py -v`
Expected: the 4 new INBOX_GIVEUP unit tests FAIL (trigger not yet coded), existing tests still PASS.

- [ ] **Step 4: Extend verify.py with INBOX_GIVEUP**

In `src/bitgn_contest_agent/verify.py`, at the end of `should_verify`, before `return reasons`, add:

```python
    # INBOX_GIVEUP — inbox skill gave NONE_CLARIFICATION without replying.
    inbox_skill = skill_name and "inbox" in skill_name.lower()
    if (
        inbox_skill
        and fn.outcome == "OUTCOME_NONE_CLARIFICATION"
        and not any(
            w.op == "write" and "outbox/" in w.path.replace("\\", "/")
            for w in write_history
        )
    ):
        # Insert in priority position: MISSING_REF > INBOX_GIVEUP > NUMERIC_MULTIREF.
        if VerifyReason.NUMERIC_MULTIREF in reasons:
            idx = reasons.index(VerifyReason.NUMERIC_MULTIREF)
            reasons.insert(idx, VerifyReason.INBOX_GIVEUP)
        else:
            reasons.append(VerifyReason.INBOX_GIVEUP)
```

And add a section builder, then wire it into `build_verification_message`:

```python
def _section_inbox_giveup(task_text: str) -> str:
    return (
        "## INBOX_GIVEUP\n"
        "You routed as an inbox task, marked outcome "
        "NONE_CLARIFICATION, and did not write any outbox reply. This "
        "usually indicates premature giveup — reconsider before "
        "finalizing:\n"
        "  - Re-read the inbox `from:` header and resolve the sender "
        "via the entity cast directly (aliases, relationship, "
        "primary_contact_email).\n"
        "  - If the task mentions a descriptor (e.g. 'design partner', "
        "'my spouse'), re-check every entity's relationship field — "
        "the descriptor may map semantically to startup_partner, wife, "
        "etc.\n"
        "  - If after that check no entity matches, re-emit "
        "report_completion with outcome OUTCOME_NONE_UNSUPPORTED "
        "(task really has no answer) or OUTCOME_NONE_CLARIFICATION "
        "with a specific clarifying question you couldn't answer from "
        "the workspace."
    )
```

Update the section-loop in `build_verification_message`:

```python
        elif r is VerifyReason.INBOX_GIVEUP:
            sections.append(_section_inbox_giveup(task_text))
```

- [ ] **Step 5: Unit tests pass**

Run: `pytest tests/test_verify_trigger.py tests/test_verify_message.py -v`
Expected: PASS — all INBOX_GIVEUP tests green, existing green.

- [ ] **Step 6: Write the INBOX_GIVEUP integration test**

Create `tests/integration/test_agent_verify_inbox_giveup.py`:

```python
"""Integration: inbox task + NONE_CLARIFICATION + no outbox write → INBOX_GIVEUP."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_agent_fires_inbox_giveup():
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        if len(calls) == 1:
            return {
                "current_state": "stuck",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "sender unclear",
                "outcome_leaning": "OUTCOME_NONE_CLARIFICATION",
                "function": {
                    "tool": "report_completion",
                    "message": "Cannot resolve the sender; need more info.",
                    "grounding_refs": [],
                    "rulebook_notes": "n/a",
                    "outcome_justification": "sender unknown",
                    "completed_steps_laconic": ["read"],
                    "outcome": "OUTCOME_NONE_CLARIFICATION",
                },
            }
        # Call 2 after verify nudge: re-emit same outcome.
        return {
            "current_state": "still stuck",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "sender still unclear",
            "outcome_leaning": "OUTCOME_NONE_CLARIFICATION",
            "function": {
                "tool": "report_completion",
                "message": "Cannot resolve the sender.",
                "grounding_refs": [],
                "rulebook_notes": "n/a",
                "outcome_justification": "sender unknown",
                "completed_steps_laconic": ["read", "re-checked"],
                "outcome": "OUTCOME_NONE_CLARIFICATION",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-03",
        task_text="take care of the next message in inbox",
        backend=backend,
        skill_name="inbox-processing",   # harness must accept + forward this
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1
    assert "INBOX_GIVEUP" in verify_events[0]["reasons"]
```

If `run_agent_with_mock_backend` doesn't accept `skill_name`, extend it to build a synthetic `RoutingDecision`:

```python
# In agent_harness.py, add this parameter:
def run_agent_with_mock_backend(
    *, task_id: str, task_text: str,
    backend: Callable[..., dict],
    skill_name: str | None = None,
) -> list[dict]:
    ...
    # Wherever Agent injects the decision.skill_name (spec §4
    # integration block), either mock a Router that returns
    # RoutingDecision(skill_name=skill_name, ...), or patch Agent so
    # the harness can pass decision directly. Simplest: subclass Agent
    # in the harness to override `_build_initial_messages` and return
    # a decision with the given skill_name.
```

Concrete override: in `agent_harness.py`, import and subclass:

```python
from bitgn_contest_agent.agent import Agent, _build_initial_messages
from bitgn_contest_agent.router import RoutingDecision


class _HarnessAgent(Agent):
    def __init__(self, *, skill_name: str | None, **kw):
        super().__init__(**kw)
        self._forced_skill_name = skill_name

    # agent.run() calls _build_initial_messages(...) at the top and
    # uses its second return value as `decision`. Monkey-patch via
    # wrapping.
    def run(self, *, task_id: str, task_text: str):
        real = _build_initial_messages
        forced = self._forced_skill_name

        def fake(*args, **kwargs):
            messages, decision = real(*args, **kwargs)
            if forced is not None:
                decision = RoutingDecision(
                    category=decision.category if decision else "INBOX_PROCESSING",
                    extracted=(decision.extracted if decision else {}),
                    confidence=(decision.confidence if decision else 1.0),
                    router_source=(decision.router_source if decision else "regex"),
                    skill_name=forced,
                    task_text=task_text,
                )
            return messages, decision

        import bitgn_contest_agent.agent as _agent_mod
        _agent_mod._build_initial_messages = fake
        try:
            return super().run(task_id=task_id, task_text=task_text)
        finally:
            _agent_mod._build_initial_messages = real
```

Swap `Agent(...)` in `run_agent_with_mock_backend` for `_HarnessAgent(skill_name=skill_name, ...)`.

If inspection reveals the real `RoutingDecision` has different field names than shown above, adjust to match.

- [ ] **Step 7: Run INBOX_GIVEUP integration test**

Run: `pytest tests/integration/test_agent_verify_inbox_giveup.py -v`
Expected: PASS — verify event fires with `INBOX_GIVEUP`.

- [ ] **Step 8: Write the multiple-reasons integration test**

Create `tests/integration/test_agent_verify_multiple_reasons.py`:

```python
"""Integration: one completion trips MISSING_REF + NUMERIC_MULTIREF; a
single verification round covers both, and the trace event lists both
reasons."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_agent_combines_reasons_in_one_round():
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        n = len(calls)
        if n == 1:
            return {
                "current_state": "reading",
                "plan_remaining_steps_brief": ["read_more"],
                "identity_verified": True,
                "observation": "new bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {"tool": "read", "path": "50_finance/purchases/bill_a.md"},
            }
        if n == 2:
            return {
                "current_state": "reading",
                "plan_remaining_steps_brief": ["total"],
                "identity_verified": True,
                "observation": "second bill",
                "outcome_leaning": "GATHERING_INFORMATION",
                "function": {"tool": "read", "path": "50_finance/purchases/bill_b.md"},
            }
        if n == 3:
            # Cite an unread path AND be scalar with 2 records → both fire.
            return {
                "current_state": "totaled",
                "plan_remaining_steps_brief": ["submit"],
                "identity_verified": True,
                "observation": "done",
                "outcome_leaning": "OUTCOME_OK",
                "function": {
                    "tool": "report_completion",
                    "message": "12 (see 40_projects/hearthline/README.md)",
                    "grounding_refs": ["40_projects/hearthline/README.md"],
                    "rulebook_notes": "n/a",
                    "outcome_justification": "sum",
                    "completed_steps_laconic": ["read", "sum"],
                    "outcome": "OUTCOME_OK",
                },
            }
        return {
            "current_state": "fixed",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "fixed",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "6",
                "grounding_refs": ["50_finance/purchases/bill_a.md"],
                "rulebook_notes": "n/a",
                "outcome_justification": "re-derived",
                "completed_steps_laconic": ["refined"],
                "outcome": "OUTCOME_OK",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-04",
        task_text="how much did vendor X charge? Number only.",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1, verify_events
    reasons = verify_events[0]["reasons"]
    assert "MISSING_REF" in reasons
    assert "NUMERIC_MULTIREF" in reasons
```

- [ ] **Step 9: Run the multiple-reasons integration test**

Run: `pytest tests/integration/test_agent_verify_multiple_reasons.py -v`
Expected: PASS — one verify event, two reasons.

- [ ] **Step 10: Write the no-infinite-loop test**

Create `tests/integration/test_verify_no_infinite_loop.py`:

```python
"""Integration: after verification fires, a re-emitted report_completion
that would trigger the same reason again must NOT fire a second verify
round."""
from __future__ import annotations

from tests.integration.agent_harness import run_agent_with_mock_backend


def test_verify_caps_at_one_round_per_task():
    calls: list[dict] = []

    def backend(messages, **_):
        calls.append({})
        # Both calls return the same bad completion (cites unread path).
        return {
            "current_state": "done",
            "plan_remaining_steps_brief": ["submit"],
            "identity_verified": True,
            "observation": "citing",
            "outcome_leaning": "OUTCOME_OK",
            "function": {
                "tool": "report_completion",
                "message": "see 40_projects/x/README.md",
                "grounding_refs": ["40_projects/x/README.md"],
                "rulebook_notes": "n/a",
                "outcome_justification": "cited",
                "completed_steps_laconic": ["done"],
                "outcome": "OUTCOME_OK",
            },
        }

    trace = run_agent_with_mock_backend(
        task_id="t-verify-05",
        task_text="tell me when",
        backend=backend,
    )
    verify_events = [r for r in trace if r.get("kind") == "verify"]
    assert len(verify_events) == 1, f"too many verify rounds: {verify_events}"
    assert verify_events[0]["changed"] is False
    # Exactly 2 backend calls: first attempt + verify retry.
    assert len(calls) == 2, calls
```

- [ ] **Step 11: Run the cap test**

Run: `pytest tests/integration/test_verify_no_infinite_loop.py -v`
Expected: PASS.

- [ ] **Step 12: Write the no-routed-preflight import test**

Create `tests/integration/test_agent_no_routed_preflight.py`:

```python
"""Integration: after Phase A deletions, the agent imports and runs
without the removed modules."""


def test_agent_imports_cleanly():
    from bitgn_contest_agent.agent import Agent  # noqa: F401


def test_deleted_modules_are_really_gone():
    import importlib
    for mod in [
        "bitgn_contest_agent.routed_preflight",
        "bitgn_contest_agent.preflight.inbox",
        "bitgn_contest_agent.preflight.finance",
        "bitgn_contest_agent.preflight.entity",
        "bitgn_contest_agent.preflight.project",
        "bitgn_contest_agent.preflight.doc_migration",
        "bitgn_contest_agent.preflight.unknown",
        "bitgn_contest_agent.preflight.canonicalize",
        "bitgn_contest_agent.preflight.response",
    ]:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{mod} still importable — Phase A incomplete")
```

- [ ] **Step 13: Run the no-routed-preflight test**

Run: `pytest tests/integration/test_agent_no_routed_preflight.py -v`
Expected: PASS.

- [ ] **Step 14: Run the whole suite**

Run: `pytest tests/ -q`
Expected: PASS — all green, no regressions.

- [ ] **Step 15: Commit**

```bash
git add src/bitgn_contest_agent/verify.py tests/test_verify_trigger.py tests/test_verify_message.py tests/integration/agent_harness.py tests/integration/test_agent_verify_inbox_giveup.py tests/integration/test_agent_verify_multiple_reasons.py tests/integration/test_verify_no_infinite_loop.py tests/integration/test_agent_no_routed_preflight.py
git commit -m "feat(verify): INBOX_GIVEUP + multi-reason + cap + deletion guard"
git push origin feat/preflight-trim-verify
```

---

## Phase D — Bench validation

### Task D1: PROD smoke (5 tasks)

- [ ] **Step 1: Source env and run a PROD smoke (first 5 leaderboard trials)**

Canonical launch per `AGENTS.md` lines 75-94 and `reference_cli_syntax.md`. Note: the PROD bench randomizes task content per run and does NOT accept a comma-separated `--tasks` list — use `--max-trials 5` to run the first 5 leaderboard trials.

```bash
set -a && source .worktrees/plan-b/.env && set +a
.venv/bin/python -m bitgn_contest_agent.cli run-benchmark \
  --benchmark bitgn/pac1-prod \
  --max-trials 5 \
  --max-parallel 3 --max-inflight-llm 6 \
  --runs 1 \
  --output artifacts/bench/<commit>_verify_smoke_p3i6_prod_runs1.json \
  --log-dir logs/smoke_verify
```

Substitute `<commit>` with the short SHA of the tip of `feat/preflight-trim-verify`.

Expected artefact: `artifacts/bench/<commit>_verify_smoke_p3i6_prod_runs1.json` and `.run_metrics.json`.

- [ ] **Step 2: Read the smoke scores**

```bash
.venv/bin/python scripts/intent_report.py artifacts/bench/<commit>_verify_smoke_p3i6_prod_runs1.json
```

Acceptance for this smoke (note — PROD task IDs are positional, not intent-stable; match tasks by `intent_head` per memory `feedback_task_compare_by_intent`):
- All 5 smoked tasks complete within `max_steps` (30) — verify adds at most 1 LLM call, which counts as 1 step against the budget.
- At least 2 tasks show a successful `verify` trace event (`kind=verify`, `changed=true` or `changed=false` with the right reason code fired). Intent targets from spec §5:
  - MISSING_REF — cited path not in read_cache (t026-shape)
  - NUMERIC_MULTIREF — scalar answer with ≥2 candidate reads (t030/t055-shape)
  - INBOX_GIVEUP — inbox skill + NONE_CLARIFICATION + no outbox write (t072-shape)
- No new failures on intents that the baseline already passed.

If fewer than 2 recoveries, investigate before running the full bench:
- Read `artifacts/ws_snapshots/<task>/trace.jsonl` for `verify` events and check `reasons` + `changed`.
- If `changed: true` but task still failed, the verification message text needs tuning.
- If `changed: false`, the trigger didn't change the model's answer — possibly the reason's prompt section is too weak.

- [ ] **Step 3: Full PROD p3i6 n=1 IF smoke is green**

This is a large resource spend. Require explicit user confirmation before starting — per project memory `feedback_no_high_parallelism_without_confirm`. **Ask the user first; do not launch unilaterally.**

```bash
set -a && source .worktrees/plan-b/.env && set +a
.venv/bin/python -m bitgn_contest_agent.cli run-benchmark \
  --benchmark bitgn/pac1-prod \
  --max-parallel 3 --max-inflight-llm 6 \
  --runs 1 \
  --output artifacts/bench/<commit>_verify_full_p3i6_prod_runs1.json \
  --log-dir logs/<commit>_prod
```

Expected artefact: `artifacts/bench/<commit>_verify_full_p3i6_prod_runs1.json`.

Acceptance criteria (spec §10):
- `server_score_total` ≥ **100** (baseline).
- ≥ 2 recoveries among {t026, t030, t055, t072}.
- No new failures on previously-passing tasks.
- `verify` trace events per task fire in ≤30 tasks total.
- avg `input_tokens` per task < 130K (target — from removing routed_preflight enumeration).

- [ ] **Step 4: If full bench passes → PR**

```bash
gh pr create --title "Preflight trim + 3-reason pre-completion verification" --body "$(cat <<'EOF'
## Summary
- Remove the `routed_preflight` dispatch pipeline and 8 per-skill preflight matcher modules (spec evidence: `match_found=True` fires 0/104 on PROD).
- Keep `prepass` (workflow-rulebook pre-read — real accelerator).
- Add `verify.py` with a 3-reason pre-completion trigger: `MISSING_REF` (t026), `NUMERIC_MULTIREF` (t030/t055), `INBOX_GIVEUP` (t072).
- Retag remaining entity-resolution hints as direct-read, not preflight-trust.
- Hard cap: ≤1 verification round per task.

Spec: `docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md`

## Test plan
- [x] Unit: `pytest tests/test_verify_classify.py tests/test_verify_trigger.py tests/test_verify_message.py tests/test_prompts.py -v` — all pass
- [x] Integration: `pytest tests/integration/test_agent_verify_*.py tests/integration/test_verify_no_infinite_loop.py tests/integration/test_agent_no_routed_preflight.py -v` — all pass
- [x] PROD 5-task smoke: ≥2 recoveries among t026/t030/t055/t072, t051 still passes
- [x] PROD `p3i6` n=1 full: server_score_total ≥ 100, no regressions

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: If full bench regresses → revert Phase C only, keep A+B**

Follow spec §8 rollback:

```bash
# Identify Phase C commits (C1, C2, C3):
git log --oneline feat/preflight-trim-verify | head
# Revert each C commit in reverse order:
git revert <C3-sha> <C2-sha> <C1-sha>
git push origin feat/preflight-trim-verify
```

Then re-run the full bench with A+B only to confirm it holds at 100/104 — no worse than baseline.

---

## Self-Review

**1. Spec coverage:**
- §3 "In scope" — 4 points:
  - Remove routed_preflight + 5 modules → Tasks A1, A2, A3, A4 ✅
  - Keep prepass → Task A2 step 4 regression check ✅
  - 3-reason verification trigger → Tasks C1, C2, C3 ✅
  - Retag preflight hints as guesses → Task B1 ✅
- §4 Architecture — `WriteOp`, `write_history`, `verify_attempts`, `append_verify`, integration point after `report_completion` and before `submit_terminal` → C1 step 5, C2 step 5, C3 step 4 ✅
- §5 Failure-mode mapping table — 3 rows, each maps to a test (test_agent_verify_missing_ref, test_agent_verify_numeric, test_agent_verify_inbox_giveup) ✅
- §6 Testing — unit (classify, trigger, message), integration (per-reason + multi + cap + deletion-guard) all present ✅
- §7 Rollout — Phases A/B/C/D map to Tasks A1-A4, B1, C1-C3, D1 ✅
- §10 Success criteria — smoke + full bench acceptance in Task D1 ✅

**2. Placeholder scan:**
- No `TBD`, `TODO`, `implement later`. ✅
- Each step has concrete code or command. ✅
- "Similar to Task N" not used — each task's code is self-contained. ✅

**3. Type consistency:**
- `VerifyReason`, `WriteOp`, `AnswerShape` defined in C1, used in C2/C3 — names match. ✅
- `should_verify` signature `(next_step, session, read_cache, write_history, task_text, skill_name)` consistent across C1 (placeholder), C2 (MISSING_REF + NUMERIC_MULTIREF), C3 (INBOX_GIVEUP) ✅
- `build_verification_message` returns `str` in all tasks. ✅
- `TraceVerify.reasons` is `list[str]` (from `[r.value for r in v_reasons]`) — aligned with integration test assertions (`assert "MISSING_REF" in verify_events[0]["reasons"]`) ✅
- `append_verify` kwargs `(at_step, reasons, changed)` consistent. ✅

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-21-preflight-trim-verify.md`.

Proceeding with subagent-driven execution (per user memory `feedback_plan_execution_mode`). Next step: dispatch implementer for Task A1.
