# Bench #1 → Bench #2 Report (2026-04-15)

**Branch:** `feat/step-validator` (working on `feat/r4-validator-correctness`)
**Model / config:** gpt-5.4 · `--max-parallel 10 --max-inflight-llm 15 --runs 1` · `bitgn/pac1-prod`
**Artifacts:** `artifacts/bench/8880fc8_r4_fix_gpt54_p10i15_prod_runs1.json` → `artifacts/bench/fcb9f3e_bench1_fixes_gpt54_p10i15_prod_runs1.json`

---

## TL;DR

|                        | bench #1 `8880fc8`         | bench #2 `fcb9f3e`            | Δ          |
|------------------------|----------------------------|-------------------------------|------------|
| Server score           | **100 / 104 (96.15 %)**    | **96 / 104 (92.31 %)**        | **−4**     |
| Local pass rate        | 100 / 104                  | 96 / 104                      | −4         |
| Tokens (in / out / rs) | 12 494 582 / 460 837 / 88 012 | 12 646 129 / 431 891 / 78 685 | noise      |
| Divergence events      | 738                        | 769                           | +31        |
| TERMINAL.ACCEPT        | 103                        | 100                           | −3         |
| TERMINAL.REJECT        | 4 (all FP)                 | 10 (7 FP, 2 TP, 1 new R1)     | +6         |
| T2 validator triggers  | 234                        | 230                           | ≈ same     |
| Rate-limit retries     | ~0                         | 19 across t036–t089           | noise only |

**Verdict:** −4 points is within single-run variance (±2 stdev per memory note),
but the degradation is **not caused by the 2 landed fixes** — it is driven
almost entirely by **PROD rotating task content per run**. The 8 bench-#2
failures are 8 *different* task instances from the 4 bench-#1 failures; the
same task_id carries a different vendor, different inbox message, different
document set each run. Comparing failure *shapes* (not ids) is the only
meaningful apples-to-apples.

---

## What was done between bench #1 and bench #2

Two orchestration-level fixes, shipped via `superpowers:brainstorming` →
`writing-plans` → `subagent-driven-development`:

1. **`e1ccf33` v0.1.11 — `load_from_env` task_timeout default 300 → 600**
   - File: `src/bitgn_contest_agent/config.py:82`
   - Completes commit `87e9a4d`'s intent. The dataclass `AgentConfig`
     field was moved 300→600 but the env-loader fallback was left at 300.
     Because `load_from_env()` is the only CLI entry point, no live bench
     had ever actually been running with 600 s. t092 in bench #1 was
     cancelled at step 19 with 4/5 mutations done — classic symptom of
     cutoff during the 5th doc migration.
   - Regression test added in `tests/test_config.py`
     (`test_load_from_env_task_timeout_default_matches_dataclass`) so the
     two defaults can't drift again.

2. **`fcb9f3e` v0.1.11 — finance-lookup skill: vendor-mismatch guardrail**
   - File: `src/bitgn_contest_agent/skills/finance_lookup.md`
   - Inserted a new bullet at Step 3 between "Primary match criteria" and
     "Date is contextual":
     > **Vendor mismatch is disqualifying.** If none of the candidate
     > records' vendor fields match the vendor named in the task, do NOT
     > answer with a number from any of them. Widen the search … before
     > falling back to `OUTCOME_NONE_CLARIFICATION`.
   - Closes bench-#1 t030's exact failure mode: agent read
     `toy_forge_pla_bundle` and `black_library_terrain_spool` when asked
     about "Filamenthütte Wien", pulled `eur_000072` from one of them,
     answered 72 instead of 24.

3. **`3d0cb06` docs** — wrote the spec
   (`docs/superpowers/specs/2026-04-15-bench1-improvements-design.md`)
   and plan (`docs/superpowers/plans/2026-04-15-bench1-improvements.md`)
   that led to the above two commits.

VERSION is now `0.1.11`.

---

## Bench #1 report (baseline, `8880fc8`)

This was the run that finished the previous validator-correctness plan
(Tasks 2–5: R1 case-insensitive + verified-absent, `measure_r4_fp.py`,
regex tier-1 decision doc).

### Score distribution
- 100 pass, 4 fail, 0 timeout at outcome level
- 4 `TERMINAL.REJECT` events on 2 tasks, both false-positives (scored 1.0):
  - `t034`: mutation integrity — agent claimed reads/searches but 0 mutations
  - `t083`: mutation integrity — 12 read/search claims, 0 mutations
  - Both are tasks whose correct outcome requires zero mutations (knowledge
    queries). The R3 rule flagged the claim-vs-reality gap; the
    `submit_anyway=True` gate correctly kept the submission, and the
    server accepted.

### Failure digest

| id   | intent                        | outcome                    | steps | mut | root cause |
|------|-------------------------------|----------------------------|-------|-----|------------|
| t030 | finance / receipt relative    | OUTCOME_OK (wrong answer)  | 10    | 0   | Answered 72 (eur from wrong vendor's file) instead of 24. Vendor mismatch not gated. |
| t041 | inbox pos 16 / Foundry bills  | OUTCOME_NONE_CLARIFICATION | 22    | 0   | Couldn't locate bills named "Foundry"; surrendered. Known always-failing inbox-pos-16 cluster (0/5 historic). |
| t066 | inbox pos 16 / multi-bill     | OUTCOME_OK (missing 2 writes) | 20  | 2   | Inbox message referenced 3 bills (hearthline, repair, juniper); agent only OCR'd juniper, claimed completion. |
| t092 | nora_migration / 5 docs       | OUTCOME_ERR_INTERNAL       | 19    | 4   | Cancelled at step 19 with 4/5 docs migrated. Effective timeout was 300 s (bug), not 600 s. |

### Arch observability summary (bench #1, 104 tasks)
- `TASK_START: 104`   `SKILL_ROUTER: 104` (tier1_regex=4 / tier2_llm=100, same as `f9613a7` baseline)
- `REACTIVE: 78` (tier-1 regex reactive skill activations during step loop)
- `VALIDATOR_T2: 234` triggers — INBOX_READ ×118, ENTITY_FINANCE_SEARCH ×46, FIRST_TRANSITION ×58, PROGRESS_CHECK ×12
- `VALIDATOR_T1: 5` · `TERMINAL_R4: 29` · `TERMINAL: 107` (of which 4 REJECT, all FP)
- `CORRECTED: 11` (T2 auto-corrected first-transition before commit)

---

## Bench #2 report (improvement head, `fcb9f3e`)

### Score distribution
- 96 pass, 8 fail, 0 timeout
- 10 `TERMINAL.REJECT` events on 5 tasks:

| task | score | rule that fired (once or twice) |
|------|-------|---------------------------------|
| t066 | 1.0 (FP) | mutation integrity — "claimed frontmatter write to existing, actually created new file" |
| t072 | 1.0 (FP) | **R1 grounding_ref** × 3 refs × 2 checks — agent cited 3 invoice files it never `read` (only appeared in a search-results blob) |
| t091 | 0.0 (TP) | mutation integrity (same "frontmatter-vs-create-new" pattern) |
| t093 | 1.0 (FP) | mutation integrity (8-doc migration, all writes were "create-new", claim was "write-frontmatter") |
| t097 | 0.0 (TP) | mutation integrity — 3 claimed mutations but actually 2 writes to same path |

The R1 hit on t072 is a **genuine grounding-integrity finding** that the
new case-insensitive + verified-absent rule correctly caught; it would not
have changed the score because `submit_anyway=True` is still the gating
policy, and the server scored the task as a pass regardless.

### Bench #2 failures (per-task digest)

| id   | intent                           | outcome                    | steps | mut | root cause |
|------|----------------------------------|----------------------------|-------|-----|------------|
| t003 | project_involvement              | OUTCOME_OK (wrong answer)  | 17    | 0   | Returned "Harbor Body / Reading Spine" instead of "Harbor Body / House Mesh". Project-listing aliasing/completeness. |
| t016 | inbox multi-bill                 | OUTCOME_OK (2 missing writes) | 21  | 2   | **Same shape as bench-#1 t066** — hearthline (eur_000050) and repair (eur_000029) bills not written. |
| t041 | inbox pos 16 / Badger bills      | OUTCOME_NONE_CLARIFICATION | 17    | 0   | Same entity-lookup shape as bench-#1 t041; inbox-pos-16 cluster. |
| t051 | project start-date              | OUTCOME_NONE_CLARIFICATION | 8     | 0   | Surrendered after 8 steps; project-record scan didn't find "health baseline project". |
| t055 | finance / CN vendor + date      | OUTCOME_OK (wrong answer)  | 13    | 0   | **Not a vendor-mismatch failure.** Vendor search matched. Agent read 2 bills (2025-12-05 and 2026-03-07), target was 76 days ago = 2026-01-13, picked 12 (the 2025-12-05 bill) instead of 6 (the 2026-03-07 bill). Date-proximity tiebreak broken. |
| t091 | inbox multi-bill                 | OUTCOME_OK (2 missing writes) | 21  | 2   | **Same shape as bench-#1 t066 & bench-#2 t016** — same two bills missing. |
| t096 | inbox outbox write               | OUTCOME_NONE_UNSUPPORTED   | 19    | 1   | Agent tried to write an outbound email, YAML validator rejected subject (needed quotes), agent then surrendered instead of retrying. |
| t097 | inbox outbox write               | OUTCOME_OK (unexpected write) | 26  | 3   | Extra outbox file written. R3 mutation-integrity rejected at step-level; submitted anyway; server scored fail for the stray write. |

### Arch observability summary (bench #2)
- `TASK_START: 104`   `SKILL_ROUTER: 104` (tier1_regex=4 / tier2_llm=100, **identical distribution** to bench #1)
- `REACTIVE: 77` (−1 vs bench #1, within noise)
- `VALIDATOR_T2: 230` triggers — INBOX_READ ×114, ENTITY_FINANCE_SEARCH ×50, FIRST_TRANSITION ×56, PROGRESS_CHECK ×10 (≈ bench #1)
- `FORMAT_VALIDATOR: 2` (new — fires on outbound YAML)
- `VALIDATOR_T1: 1` · `TERMINAL_R4: 29` · `TERMINAL: 110` (of which 10 REJECT — 7 FP + 2 TP + 1 "valid integrity finding that still passed")
- `CORRECTED: 10` (≈ bench #1)

---

## Did the targeted fixes land correctly?

### Fix 1 — timeout 300 → 600
- bench-#2 `t092` now passes: outcome OUTCOME_OK, 21 steps, **no timeout** (task content in b2 was different — 3 docs instead of 5 — but it still proves the budget is now applied).
- Zero `OUTCOME_ERR_INTERNAL` / `timed_out=True` in bench #2.
- Regression test pins the two defaults together; future drift will be caught in CI.
- **Landed correctly. Load-bearing for doc-migration tasks.**

### Fix 2 — finance vendor-mismatch guardrail
- bench-#2 `t030` (CN vendor 深圳市星河玩具配件, magnet pack, 58 days ago): passes. The skill body change is live (verified via `skill_loader` round-trip before shipping).
- bench-#2 `t055` (CN vendor 深圳市海云电子, relay modules, 76 days ago) fails — but **the guardrail did not fire** because the vendor field DID match. The failure is at a later step: among two matching bills (2025-12-05 line_eur=12 and 2026-03-07 line_eur=6), the agent picked the wrong one. Skill body says "date is contextual, NOT a strict filter"; agent treated both as valid and picked the earlier one. Fix is orthogonal to this new failure mode.
- The "guardrail too strict" predicted risk did NOT materialise. In the entire bench #2 there is no finance task where a vendor match was widened-then-surrendered (no `OUTCOME_NONE_CLARIFICATION` on a finance-lookup task).
- **Landed correctly. Did not cause regressions.**

---

## Degradation reason — what explains the −4

The bench scoring data answers this mechanically:

1. **PROD tasks are randomised per run.** Same `task_id` carries different
   instruction each run. A few side-by-side examples:

   | id   | bench #1 instruction                     | bench #2 instruction                 |
   |------|------------------------------------------|--------------------------------------|
   | t003 | "Warhammer friend" projects              | "walking buddy" projects             |
   | t030 | Filamenthütte Wien / PLA spool / 50d     | 深圳市星河玩具配件 / magnet pack / 58d |
   | t055 | Haushaltshilfe München / gasket tape / 43d | 深圳市海云电子 / relay modules / 76d |
   | t092 | 5 docs to NORA                            | 3 docs to NORA                        |

   Therefore comparing b1-t030 vs b2-t030 tells us nothing about a fix;
   we must compare failure **shapes**.

2. **Failure shape tally (the real comparison):**

   | shape                         | b1  | b2  |
   |-------------------------------|-----|-----|
   | finance / wrong answer        | 1   | 1   |
   | inbox clarification surrender | 1   | 1   |
   | inbox missing multi-bill writes | 1 | **2** |
   | project wrong answer          | 0   | 1   |
   | project surrender             | 0   | 1   |
   | outbox unexpected write       | 0   | 1   |
   | outbox YAML-rejected & surrender | 0 | 1   |
   | doc migration timeout         | 1   | 0   |

   Stable shapes (finance-wrong, inbox-surrender): 2 in each run.
   Shape we fixed (doc-migration timeout): **−1 → 0**. Net win.
   Shapes that got WORSE:
   - Inbox missing multi-bill writes: **1 → 2** (both hitting the exact same
     two bills: `hearthline_sensor_bundle.md` eur_000050 and
     `repair_ledger_filter_order.md` eur_000029). This is a systematic
     gap, not variance: PROD randomises the inbox message but the "3-bill
     batch OCR" pattern recurs and the agent keeps processing only one.
   - Project tasks: **0 → 2** (one wrong answer, one surrender). Both are
     `project-involvement` family, which suggests the skill has weak
     completeness guarantees. Both are variance-class: no systemic fix in
     either plan.
   - Outbox-adjacent: **0 → 2** (one extra write, one YAML-blocked
     surrender). Both are new-shape failures in bench #2, neither
     addressed by any fix we shipped.

3. **Rate-limit noise check.** Bench #2 had 19 httpx retries (vs ~0 in
   bench #1). All retries hit tasks t036–t089. **None** of the 7 new
   failures (t003, t016, t051, t055, t091, t096, t097) received retries.
   Rate-limit noise is not the cause.

4. **Arch-level behaviour is virtually unchanged.** Router distribution is
   identical (4 tier1_regex + 100 tier2_llm). T2 validator firing counts
   are within ±4 of bench #1. REACTIVE count is −1. The code paths we
   touched in this iteration (config default, skill body) do not affect
   routing, validation, or reactive nudges.

**Conclusion: the −4 is PROD task rotation + variance. No shipped change
in this iteration causes a regression.**

---

## Arch interventions — full analysis

### TERMINAL REJECT comparison

The most informative arch delta. Of the 10 bench-#2 REJECTs:

- **6 on t072**: R1 rule ("grounding_ref never successfully read") — 3
  distinct refs × 2 checks (pre-commit + post-commit) = 6 events. The
  3 claimed refs were `northstar_backfill_{alpha,beta,gamma}.md` which
  the agent saw in a search-results blob at step 19 but never opened
  with `read`. **This is the R1 rule working exactly as designed** after
  Task 3 of the earlier plan. The rule did NOT force-reject (per `submit_anyway=True`);
  the submission went through, the server accepted with 1.0.

- **2 on t066** (FP, scored 1.0), **2 on t091** (TP, scored 0.0),
  **2 on t093** (FP, scored 1.0): same rule — mutation integrity says
  "agent claimed YAML frontmatter write to *existing* bill file; actual
  mutations show *new file creation*". This is an LLM-reported-vs-actual
  gap in how OCR tasks get reported. Shape is systemic — same phrasing
  on 3 different tasks. All three are `inbox_read` bill-OCR tasks.

- **2 on t097** (TP, scored 0.0): mutation integrity — "3 claimed
  mutations vs 2 actual writes to same path". Different shape from the
  OCR family; this is the extra-outbox-write case.

### R1 normalization check on t072

To confirm R1 is not leaking false positives, I replayed the trace:

```
Validator check (current normalization = lower-only):
  OK  AGENTS.MD                                               (seen=True)
  OK  50_finance/invoices/2026_02_14__…__inv_0004__…md      (seen=True)
  REJECT  50_finance/invoices/…__inv_0003__…_gamma.md        (seen=False absent=False)
  REJECT  50_finance/invoices/…__inv_0002__…_beta.md         (seen=False absent=False)
  REJECT  50_finance/invoices/…__inv_0001__…_alpha.md        (seen=False absent=False)
  …

With additional lstrip("/") normalization: identical result.
```

No leading-slash drift. No casing drift. The 3 refs are genuinely
uncited. **No additional fix needed; R1 is behaving correctly.** The
"`submit_anyway=True`" policy keeps the submission despite the flag —
which the bench-#1 validator-correctness plan (Task 4 measurement)
explicitly chose to preserve until more evidence.

### Inbox multi-bill missing-writes is the single biggest stable loss-bucket

Both bench-#1 t066 and bench-#2 t016/t091 show the agent OCR'ing the
Juniper bill only and ignoring the hearthline + repair bills that the
inbox message referenced. That is 3 tasks (= 3 points) across the 2
runs. If this shape were fixed, bench #2 would be 98/104. This is the
most productive single target for the next iteration.

---

## Artifacts

| file                                                                               | purpose                              |
|------------------------------------------------------------------------------------|--------------------------------------|
| `artifacts/bench/8880fc8_r4_fix_gpt54_p10i15_prod_runs1.json`                      | bench #1 raw + server scores         |
| `artifacts/bench/fcb9f3e_bench1_fixes_gpt54_p10i15_prod_runs1.json`                | bench #2 raw + server scores         |
| `logs/20260414_214354/`                                                            | bench #1 per-task trace JSONLs       |
| `logs/20260414_222014/`                                                            | bench #2 per-task trace JSONLs       |
| `/tmp/b1_failures.md`                                                              | bench #1 failure digest (4 tasks)    |
| `/tmp/b2_failures.md`                                                              | bench #2 failure digest (8 tasks)    |
| `docs/superpowers/specs/2026-04-15-bench1-improvements-design.md`                  | fix design                           |
| `docs/superpowers/plans/2026-04-15-bench1-improvements.md`                         | fix plan                             |
| `docs/decisions/2026-04-14-keep-regex-tier1.md`                                    | regex tier-1 decision (from b1 plan) |

---

## Recommendations / next steps

1. **Accept bench #2 as within noise, no rollback.** Both fixes landed
   cleanly; no regression is attributable to them.
2. **Next-iteration target: inbox multi-bill OCR completeness.** This is
   the single biggest stable loss-bucket (3 of 12 failures across 2
   runs). Design idea: have the inbox-triage skill enumerate *all*
   referenced bills as a task-plan object, then enforce
   count-of-mutations ≥ count-of-referenced-bills before
   `report_completion(OUTCOME_OK)` can fire.
3. **If re-running to confirm:** use `--runs 3` on both commits
   (`8880fc8` and `fcb9f3e`). ±2 stdev at n=1 becomes ±1 at n=3.
4. **Re-measure R1 TP/FP on the bench-#2 trace:**
   ```
   uv run python scripts/measure_r4_fp.py logs/20260414_222014
   ```
   Expected based on spot-check: 1 R1 event (t072) which is a real
   integrity finding on a task that passed anyway. `submit_anyway=True`
   stays the right choice for now; flip to `force-reject` only after
   we see an R1 TP that would have been caught cheaper via rejection.
5. **Inbox-pos-16 cluster still unresolved** (1 failure each bench:
   b1-t041 Foundry, b2-t041 Badger). Listed as out-of-scope in the
   bench-#1 improvement spec; still out of scope until dedicated
   investigation.
