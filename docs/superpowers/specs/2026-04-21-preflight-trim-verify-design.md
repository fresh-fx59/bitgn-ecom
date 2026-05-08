# Preflight Trim + Verification Discipline — Design

**Branch:** `feat/preflight-trim-verify`
**Baseline:** `main @ 308b676` — 100/104 PROD pass (server_score 100.0/104, mean 0.9615)
**Target:** reduce preflight complexity and context bloat; address the 4 observed PROD failures without per-wording patches.

---

## 1. Goal

Make the agent's preflight subsystem earn its place by evidence: drop the parts that never fire, keep the parts that demonstrably accelerate tasks, and add one narrowly-scoped verification pass for the answer shapes most prone to error.

One sentence: **remove dead matcher code, keep the rulebook pre-read, add a 3-reason pre-completion verification trigger targeting observed failure shapes, and strip "trust the preflight entity" language.**

---

## 2. Evidence (from PROD run @ 2da4b34, 104 tasks)

| Measurement | Value |
|---|---|
| Tasks with `routed_preflight.match_found=True` | **0 / 104** |
| Tasks whose agent `current_state` cites preflight output | ~18 / 104 |
| Tasks whose agent cites the workflow rulebook (prepass-loaded) | 25+ / 104 |
| Prepass ops per task | ~161 avg |
| Prepass bytes per task | ~150KB avg |
| Agent reads that duplicate prepass reads | **98.4%** (4613 / 4690) |
| Avg input tokens per task | 157K |
| Failures (OUTCOME != OK, scored 0) | t026, t030, t055, t072 |

### Observations per failure

- **t026** — asked for start date of "the project our home assistant setup". Two project folders matched (`house_mesh`, `hearthline`). Agent read only `house_mesh` and answered. Classic multi-candidate disambiguation skip.
- **t030 / t055** — "how much did 深圳市海云电子 charge me in total for the line item relay modules 76 days ago? Number only". Two bills matched, expected **6** (single quantity) but agent returned **12** (either summed both or misread). Numeric aggregation/filter error.
- **t072** — "Take care of the next message in inbox". Preflight resolved the referenced entity to **Jana**; correct answer was **Nina** (startup_partner). Agent blindly trusted preflight, searched Jana, found nothing, returned `NONE_CLARIFICATION`.

### Observations per success pattern

- **Workflow-doc pre-read** (prepass) is the real accelerator. `99_system/workflows/AGENTS.MD` → `inbox-processing-v2-update.md` is cited in 25+ inbox tasks as the reason the agent knows what to do.
- **Single-item inbox pointer** from `preflight_inbox` (e.g. "one open item at 00_inbox/297_next-task.md") saves 1–2 steps in ~6 inbox tasks.
- **Finance bill candidate** from `preflight_finance` is cited in 4 tasks as a starting point.
- Everything else in `routed_preflight.py` produces "here are candidates, figure it out" — information the agent would produce itself in 1–2 `list`/`search` calls.

---

## 3. Scope

### In scope

1. Remove `routed_preflight` dispatch pipeline and the five per-skill preflight modules.
2. Keep `prepass` (tree + AGENTS.MD crawl + workflow discovery) unchanged.
3. Add a **pre-completion verification trigger** with three reason codes, each mapped 1:1 to an observed PROD-baseline failure:
   - `MISSING_REF` — answer cites a path not in the agent's read history. (Motivating failure: t026.)
   - `NUMERIC_MULTIREF` — scalar (number/date) answer with ≥2 candidate records read. (Motivating failures: t030, t055.)
   - `INBOX_GIVEUP` — inbox-routed task emits `NONE_CLARIFICATION` without writing to outbox. (Motivating failure: t072.)
4. Retag any remaining preflight-style hints as **guesses**, not canonical facts.

**Deferred to v2** (see §9 and §11): `ATTACHMENT_GAP`, `OUTBOX_INTEGRITY`, `FILE_OP_MISSING`, `SECURITY_CHECK`, `ANSWER_PRECISION`. These were derived from failure shapes observed across 9 PROD runs, but most of those runs used pre-`2da4b34` code that has since shipped disambiguation + sender-exclusion fixes. Adding them now is speculation, not evidence. v1 ships the 3 reasons that match current-baseline failures; v2 expands based on measured gaps in v1 results.

### Out of scope (explicitly deferred)

- CodeAct / sandboxed Python execution — no observed failure requires arbitrary code.
- Blanket Reflexion before every `report_completion` — cost/benefit negative; targeted trigger is enough.
- Rubric-based evaluator — without veto power it's decorative; with veto it risks blocking correct answers on ambiguous rubrics.
- Router changes — the tier-1/tier-2 classifier is not the bottleneck and stays as-is.

---

## 4. Architecture after changes

### Prepass (unchanged)

```
run_prepass:
  tree /                        # 1 op
  read AGENTS.md                # 1 op
  context                       # 1 op
  tree / (re-read after AGENTS) # 1 op
  list each top-level folder    # ~8 ops
  read each folder's AGENTS.MD  # ~8 ops
  read workflow docs            # ~5 ops
  discover WorkspaceSchema      # derived from tree + AGENTS content
```

This layer produces the `WorkspaceSchema` object (entities_root, finance_roots, projects_root, inbox_root) and the workflow-rulebook context. Both are real accelerators — keep as-is.

### Routed preflight (REMOVED)

Delete:
- `src/bitgn_contest_agent/routed_preflight.py` (223 lines)
- `src/bitgn_contest_agent/preflight/inbox.py` (367 lines)
- `src/bitgn_contest_agent/preflight/finance.py` (136 lines)
- `src/bitgn_contest_agent/preflight/entity.py` (482 lines)
- `src/bitgn_contest_agent/preflight/project.py` (311 lines)
- `src/bitgn_contest_agent/preflight/doc_migration.py` (118 lines)
- `src/bitgn_contest_agent/preflight/unknown.py` (105 lines)
- `src/bitgn_contest_agent/preflight/canonicalize.py` (36 lines)
- `src/bitgn_contest_agent/preflight/response.py` (14 lines)
- Corresponding `Req_Preflight*` / `Rsp_Preflight*` classes in `schemas.py`
- `_dispatch_routed_preflight` helper and call-site in `agent.py`
- Harness-side preflight adapter hooks in `adapter/`
- Frontmatter fields `preflight:` and `preflight_query_field:` from each skill YAML

Keep:
- `src/bitgn_contest_agent/preflight/schema.py` — `WorkspaceSchema` is still used by prepass.

### Verification trigger (NEW)

Location: `src/bitgn_contest_agent/verify.py` (new file, ~200 lines for the 3 v1 reasons).

The verification trigger fires before `report_completion` is emitted, covering three distinct risk shapes — one per observed PROD-baseline failure:

**Risk 1 — reference-read discipline (`MISSING_REF`).**
Motivating failure: **t026** (project disambiguation skip). Scorer penalty from prior runs: *"answer missing required reference '40_projects/2026_04_01_hearthline/README.MD'"*. Verification confirms every path cited in the answer appears in the agent's read history for this run; fires when there's a gap.

**Risk 2 — scalar answer with multiple candidates (`NUMERIC_MULTIREF`).**
Motivating failures: **t030, t055** (vendor-filter aggregation). Scorer penalty: *"answer is incorrect. Expected: '6'"*. Fires when the answer classifies as NUMERIC or DATE AND the agent read ≥2 records of the same shape (bills, projects, etc.). Verification asks the model to re-derive the answer citing one evidence path per numerical component.

**Risk 3 — premature giveup on inbox action (`INBOX_GIVEUP`).**
Motivating failure: **t072** (preflight-resolved wrong entity, agent blindly trusted, gave up). Fires when the skill was an inbox handler, outcome is `NONE_CLARIFICATION`, and no outbox write exists in write_history. Verification asks the agent to re-resolve the sender + check every entity alias/relationship before concluding "no match".

Contract:
```python
def classify_answer_shape(next_step: NextStep, task_text: str) -> AnswerShape:
    """Return one of: NUMERIC, DATE, PATH_LIST, MESSAGE_QUOTE, ACTION_CONFIRMATION,
    NONE_CLARIFICATION, FREEFORM. Regex + task-text heuristics, no LLM."""


def should_verify(
    next_step: NextStep,
    session: Session,
    read_cache: dict[str, str],
    write_history: list[WriteOp],
    task_text: str,
    skill_name: str | None,
) -> list[VerifyReason]:
    """Return list of reasons when verification should fire (possibly multiple).

    All three reasons are checked independently. Returned list is ordered
    by priority:
      1. MISSING_REF         (scorer-hard-fail shape — address first)
      2. INBOX_GIVEUP        (behavioral correctness — only triggers on NONE_CLARIFICATION)
      3. NUMERIC_MULTIREF    (answer-content correctness)

    Callers should address all triggered reasons in a single verification
    message (concatenated sections), not multiple round-trips.
    """


def build_verification_message(
    reasons: list[VerifyReason],
    next_step: NextStep,
    read_cache: dict[str, str],
    write_history: list[WriteOp],
    task_text: str,
) -> str:
    """Produce a single user message covering every triggered reason.

    Format:
      <intro paragraph — "Before submitting, address the following checks.">
      <section per reason, each with concrete evidence:>
        ## MISSING_REF
        Your answer cites: <paths from answer>
        You read this run: <paths from read_cache>
        Gap: <paths cited but not read>

        ## NUMERIC_MULTIREF
        You read N candidate records: <list>
        Your scalar answer: <value>
        Re-derive citing one evidence path per numerical component.

        ## INBOX_GIVEUP
        Inbox task, outcome=NONE_CLARIFICATION, no outbox write.
        Re-check sender resolution: aliases, relationships, email match.

      <closing instruction: "Re-emit report_completion only after addressing
       these. If evidence confirms your answer, keep it.">

    Templates are fixed Python f-strings, not LLM-authored — deterministic
    per evidence state.
    """
```

Integration point: in `agent.py` main loop, after the model returns `report_completion` but **before** the outcome is emitted to the harness:

```python
reasons = should_verify(
    next_step, session, read_cache, write_history, task_text,
    skill_name=decision.skill_name if decision else None,
)
if reasons:
    messages.append(Message(
        role="user",
        content=build_verification_message(
            reasons, next_step, read_cache, write_history, task_text,
        ),
    ))
    # One more backend call covering all triggered reasons at once.
    # If the model emits a DIFFERENT report_completion (answer, attachments,
    # outcome, or a tool call instead of completion), use that. If it
    # re-emits the same completion, accept it.
    # Hard cap: ≤1 verification round per task.
    trace_writer.append_verify(reasons=[r.name for r in reasons], changed=bool(differs))
```

**Tracked state the trigger reads from:**
- `read_cache: dict[path → content]` — every file the agent's own steps read (already exists in `agent.py:320`).
- `write_history: list[WriteOp]` — every write/delete/move the agent performed. New accumulator; append on every `pcm_op` with op in `{"write", "delete", "move"}`. Each entry carries `path`, `op`, `step`, and for writes the resulting content (so we can re-parse YAML frontmatter of outbox files without re-reading from the harness).

**Cost estimate (v1, 3 reasons):**

| Reason | Expected trigger rate/run | Motivating baseline failure |
|---|---|---|
| MISSING_REF | 2-5 tasks | t026 |
| NUMERIC_MULTIREF | 10-15 tasks | t030, t055 |
| INBOX_GIVEUP | 2-4 tasks | t072 |

Many tasks may fire multiple reasons but use only one round-trip. Expected overhead: ≤20 tasks/run fire verification → ~1-2% of total LLM calls. Well below the 30-step per-task budget (verification counts as 1 step).

### Trust-signal fix (NEW)

Currently the preflight blob presents entity resolution as a fact:

```
preflight indicates sender entity resolved to Jana (startup_partner).
```

After the Change 3 fix (applies to whatever preflight-shaped text remains — currently just the prepass-derived inbox pointer):

```
preflight GUESS (not verified): the inbox's `from:` header suggests Jana.
Before acting on any entity inferred from preflight, re-open the inbox
file and confirm `from:` / entity descriptors directly.
```

This is a prompt-template change only — no new code paths. It lives in `prompts.py`.

---

## 5. Failure-mode mapping

v1 covers the three shapes matching current-baseline failures (PROD @ 2da4b34, 4 failing tasks):

| Failure pattern | Example scorer detail | Trigger that fires | How it addresses the failure |
|---|---|---|---|
| Answer cites unread file | t026: answer referenced hearthline README without reading it | `MISSING_REF` | Lists cited-vs-read paths; forces opening the gap before answering. |
| Scalar answer wrong | *"answer is incorrect. Expected: '6'"* — t030/t055 | `NUMERIC_MULTIREF` | Re-derives from both candidates citing vendor + date filter. |
| Inbox gives up without acting | t072 NONE_CLARIFICATION after trusting preflight | `INBOX_GIVEUP` | Re-resolve sender + check every entity before concluding "no match". |

**Deferred failure shapes** (observed in older runs; may already be addressed by fixes shipped in `2da4b34`, `29bbca5`, `4a1475c`; revisit in v2 if they re-appear in v1 results): outbox YAML malformed, outbox wrong attachment, missing file write/delete, security outcome wrong, answer precision extras.

### Answer-shape classifier (`classify_answer_shape`)

Non-LLM heuristics. Used both for trigger selection and for the verification message template:

| Shape | Matches when |
|---|---|
| `NUMERIC` | answer matches `^-?\d+(\.\d+)?$` OR task contains `"number only"` / `"Answer with a number"` |
| `DATE` | answer matches one of `YYYY-MM-DD`, `DD-MM-YYYY`, `MM/DD/YYYY`, `Month DD, YYYY`, OR task contains `"Date only"` / `"Answer YYYY-MM-DD"` / `"format"` + any date-token |
| `PATH_LIST` | answer contains ≥1 `/`-separated token with extension (e.g. `50_finance/...`), one per line |
| `MESSAGE_QUOTE` | task contains `"Quote"` / `"exact message"` / `"return only the"` + "message/text" |
| `ACTION_CONFIRMATION` | task contains `"take care of"` / `"handle"` / `"work"` + inbox; answer is empty or status string |
| `NONE_CLARIFICATION` | next_step `outcome_leaning == "NONE_CLARIFICATION"` |
| `FREEFORM` | none of the above |

The classifier is deterministic and cheap. Failing to classify defaults to `FREEFORM` → no trigger.

---

## 6. Testing strategy

### Unit tests (added alongside implementation)

- `tests/test_verify_classify.py` — `classify_answer_shape` matrix covering each shape (NUMERIC / DATE / PATH_LIST / MESSAGE_QUOTE / ACTION_CONFIRMATION / NONE_CLARIFICATION / FREEFORM) with positive + negative cases.
- `tests/test_verify_trigger.py` — `should_verify` decision matrix, one section per reason:
  - `MISSING_REF`: answer cites path ∉ read_cache → fires; all paths ∈ read_cache → no fire; freeform prose answer with no paths → no fire.
  - `NUMERIC_MULTIREF`: (NUMERIC/DATE answer, seen_refs ≥ 2) → fires; (FREEFORM + refs) → no fire; (NUMERIC + 1 ref) → no fire.
  - `INBOX_GIVEUP`: inbox skill + NONE_CLARIFICATION + no outbox writes → fires; inbox + OK → no fire; non-inbox skill → no fire.
- `tests/test_verify_message.py` — `build_verification_message` returns a single multi-section message with correct section for each triggered reason. Test for 1 reason and 3 reasons.

### Integration tests

- `tests/integration/test_agent_verify_numeric.py` — mock backend returns `report_completion(answer="12")` after reading 2 bills; assert verification injected; second call invoked; trace has `verify` event with `reasons=["NUMERIC_MULTIREF"]`.
- `tests/integration/test_agent_verify_missing_ref.py` — mock agent cites path never read; assert `MISSING_REF` fires.
- `tests/integration/test_agent_verify_inbox_giveup.py` — mock inbox-routed task emits NONE_CLARIFICATION with no outbox write; `INBOX_GIVEUP` fires.
- `tests/integration/test_agent_verify_multiple_reasons.py` — one task triggers 2+ reasons at once; single verification call; all sections in the prompt.
- `tests/integration/test_agent_no_routed_preflight.py` — agent runs end-to-end without removed modules; no ImportError.
- `tests/integration/test_verify_no_infinite_loop.py` — second call re-emits same `report_completion`; assert no third call.

### Regression (bench)

1. Local smoke on a tiny workspace (`tests/preflight/fixtures/tiny_ws`) — still exists for prepass/WorkspaceSchema tests, which continue to pass.
2. PROD 5-task smoke — subset of `t026, t030, t055, t072, t051` (the four failures plus one known passing baseline).
3. Full PROD run `p3i6` n=1 once smoke is green. **Acceptance:** server_score_total ≥ 100 (same as baseline) AND at least 2 of {t026, t030, t055, t072} recover.

### Test fixtures to drop

`tests/preflight/test_inbox.py`, `tests/preflight/test_entity.py`, and other preflight module tests exercise code being deleted. They go away with the modules.

---

## 7. Rollout plan

All work on `feat/preflight-trim-verify`. Single PR when green.

1. **Phase A — Delete dead code** (one commit)
   - Remove `routed_preflight.py`, 7 preflight modules, related schemas, frontmatter fields, adapter hooks, `agent.py` dispatch helper.
   - Keep `preflight/schema.py` (used by prepass).
   - Run unit tests; ensure `pytest tests/` passes after removal.

2. **Phase B — Prompt fix** (one commit)
   - Update `prompts.py` to re-phrase any remaining preflight-derived hints as guesses.
   - Unit test asserting the new phrasing.

3. **Phase C — Verification trigger** (split across 3 TDD commits)
   - C1: Scaffolding — `classify_answer_shape`, `VerifyReason` enum, `WriteOp` dataclass, `write_history` accumulator in `agent.py`, `trace_writer.append_verify`, integration wire-up (no reasons fire yet). Tests: classifier matrix, plumbing, no-op integration.
   - C2: `MISSING_REF` + `NUMERIC_MULTIREF` (both share answer-parsing plumbing). Tests + integration.
   - C3: `INBOX_GIVEUP` + multi-reason combination test + single-retry cap test. Tests + integration.

4. **Phase D — Bench validation**
   - PROD smoke on t026, t030, t055, t072, t051 (run-level n=1, small parallelism).
   - If ≥2 of the 4 failures recover and no new regressions: full PROD `p3i6` n=1.
   - If full run ≥ 100/104: open PR, merge, delete branch.

---

## 8. Rollback plan

If any phase regresses below baseline (100/104):

- **Worst case:** `git revert` the phase's commit; the baseline is preserved on `main @ 308b676` and every commit is pushed per user policy.
- **Partial rollback:** Phase A (deletion) and Phase C (verification) are independent — if Phase C misbehaves, revert just that commit while keeping the cleanup from Phase A.
- **No data loss risk:** the preflight modules being deleted do not own any persistent state; all their effects were ephemeral context injection per task.

---

## 9. Risks and counter-arguments

**Risk 1 — losing the 18 "preflight cites" we observed.**
Those 18 tasks explicitly referenced preflight in their `current_state`. Eight were the inbox file-pointer (still available post-delete via workflow docs + a single `list` call). The other ~10 cited finance bill narrowing, which is 1–2 `list/search` ops the agent already does anyway. Worst case, +1–2 steps per affected task. Step budget (30) has headroom.

**Risk 2 — verification trigger false positives.**
If the model flips from a correct answer to an incorrect one after re-derivation, we regress. Mitigation: the verification prompt is reason-specific and asks the model to *cite evidence paths*; if evidence matches the original answer, keep it. Log both. Initial deployment with n=1 bench watches for exactly this.

**Risk 3 — answer-shape classifier too narrow.**
Task wording for dates varies (`YYYY-MM-DD`, `DD-MM-YYYY`, `MM/DD/YYYY`, `Month DD, YYYY`, free-text). The regex + task-text heuristics will cover common shapes; rare formats silently skip verification (false negative). Acceptable — better to skip verification than to block a correct answer on a misclassified shape.

**Risk 4 — hidden coupling with adapter.**
The adapter may depend on `Req_Preflight*` schemas. Phase A must remove those dependencies in lockstep. A deletion sweep with `grep -r Req_Preflight src/` is required as a dry-run before committing.

**Risk 5 — `MISSING_REF` may undercover.**
Our trigger only checks paths *cited in the answer text* that weren't read. That's the direct signal from t026. It won't catch cases where the scorer silently requires a file the agent never mentioned either. Acceptable — a subset of real coverage is better than no check.

**Risk 6 — `INBOX_GIVEUP` misfires on legitimate NONE_CLARIFICATION.**
Some inbox tasks legitimately lack information. Our trigger only fires on `NONE_CLARIFICATION` (agent explicitly asking for more info) not `NONE_UNSUPPORTED`. Even so, the verification is *advisory* — the agent can re-emit the same NONE_CLARIFICATION after re-checking. Budgeted ≤1 retry.

**Risk 7 — verification message pushes the agent over the step budget.**
The verification call is counted against `max_steps` (30). Tasks already at step 29 skip verification (hard cap check). Separately, tasks that trigger verification tend to be short-path cases (numeric answer, inbox resolution) — average step count at trigger time observed at 8-12, well below the budget.

---

## 10. Success criteria

1. `src/bitgn_contest_agent/preflight/` directory shrinks from 9 files to 1 (`schema.py` only).
2. `routed_preflight.py` is gone; no references remain.
3. All existing tests pass (after removing tests that exercise deleted code).
4. PROD `p3i6` n=1 reaches **≥ 100/104** server_score (baseline), with **≥ 2 recoveries** among `{t026, t030, t055, t072}`.
5. Verification adds ≤2% to total LLM calls (budget: ≤20 tasks/run × 1 extra call ≈ 1-2% of total).
6. Avg input tokens per task drops materially (baseline 157K; target <130K — removing 55 routed_preflight ops worth of bill/entity enumeration).
7. No new regressions on the 100 currently-passing tasks (verification must not flip correct answers to incorrect).

---

## 11. What this is NOT a bet on

- This spec does not bet on "one strategy fixes all failures." It removes known dead complexity, keeps known value, and adds one verification pass targeted at the three observed PROD-baseline failure shapes.
- If the verification trigger doesn't move the needle, we revert Phase C and still keep the preflight cleanup.
- v1 deliberately does *not* attempt to address failure shapes from older code versions. If those shapes re-appear in v1 results, a v2 spec revisits `ATTACHMENT_GAP`, `OUTBOX_INTEGRITY`, `FILE_OP_MISSING`, `SECURITY_CHECK`, `ANSWER_PRECISION` with fresh evidence.
