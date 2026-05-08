# Bench #1 Improvements — Design (2026-04-15)

## Context

Bench #1 on commit `8880fc8` (feat/r4-validator-correctness HEAD) using
`gpt-5.4` at `--max-parallel 10 --max-inflight-llm 15` against
`bitgn/pac1-prod`:

- **Server score: 100 / 104 (96.15%)** — strong; baseline 95, peak 101
- **4 failures:** t030, t041, t066, t092

## Failure diagnosis

| Task | Intent | Outcome | Root cause |
|------|--------|---------|-----------|
| t030 | receipt_total_relative | OUTCOME_OK wrong | Agent answered `72` (a different vendor's `eur_000072` invoice id) instead of `24`. Vendor mismatch not caught. Asked about "Filamenthütte Wien / PLA spool", agent read `toy_forge_pla_bundle` + `black_library_terrain_spool`. The finance-lookup skill tells the agent to use vendor + item as primary match but does not forbid answering from a record whose vendor does not match. |
| t041 | inbox_en position 16 | OUTCOME_NONE_CLARIFICATION | Known "always-failing" inbox position (per AGENTS.md memory, 0/5 historical). Cross-lane / trust-boundary / unsupported-channel. Skipping — no tractable fix in scope. |
| t066 | inbox_en position 16 | OUTCOME_OK missing writes | Inbox message referenced 3 bills; agent processed only the Juniper one, reported OK. Existing prompt already warns "do NOT silently complete a partial task"; additional prompt text unlikely to help. Same "position 16 always-fails" cluster as t041. |
| t092 | nora_migration | OUTCOME_ERR_INTERNAL timeout | 5-file document migration terminated by cancel at step 19 with 4 of 5 mutations done. Effective task timeout is 300s (not 600s as intended). **Bug:** `config.py:82` `_int_env("TASK_TIMEOUT_SEC", 300)` default never got bumped when dataclass default moved 300 → 600 in commit `87e9a4d`. The dataclass default is unused because `load_from_env()` is the only entry point. |

## Tractable fixes — this iteration

Two fixes, both orchestration-level (config + skill body). No new
code paths, no new dependencies.

### Fix 1 — align `load_from_env` default with dataclass default

`src/bitgn_contest_agent/config.py:82` — change
`_int_env("TASK_TIMEOUT_SEC", 300)` to `_int_env("TASK_TIMEOUT_SEC", 600)`.

**Why:** completes commit `87e9a4d`'s intent. The earlier commit bumped
the dataclass default but missed the env loader, so no running benchmark
has ever had the intended 600s timeout. t092 took 19 steps of progress
and was cancelled; an extra ~300s budget likely finishes it.

**Risk:** tasks that would have hit the budget earlier now have more
room to hang. Mitigations:
  - `max_steps=40` bound is unchanged (hard step cap)
  - No live task has ever benefited from cutting at 300s — failures
    at 300s tend to be agents that were still productive

**Rejected alternative:** introduce a per-skill timeout multiplier. Too
much new machinery for one known-failing task.

### Fix 2 — finance-lookup skill: reject vendor-mismatched records

`src/bitgn_contest_agent/skills/finance_lookup.md` — insert a
vendor-match guardrail into Step 3 (Cross-Validate and Select):

> **Vendor mismatch is disqualifying.** If none of the candidate
> records' vendor fields match the vendor named in the task, do NOT
> answer with a number from any of them. Widen the search (Step 2.2
> partial match, Step 2.3 different artifact, Step 2.4 broader listing)
> before falling back to `OUTCOME_NONE_CLARIFICATION`. A numeric answer
> pulled from a different vendor's invoice is worse than asking for
> clarification.

**Why:** t030's bug was not line-item extraction (the skill already
covers that) but rather that the agent picked the closest-looking
filename when the requested vendor didn't exist in its search results.
Making vendor match disqualifying forces a wider search or a principled
surrender.

**Risk:** agents might now surrender on tasks where the vendor name in
the record is a variant/alias. Mitigations:
  - Step 2.2's "partial match" fallback already allows shorter /
    alternate spellings — guardrail says "do NOT answer from a
    mismatch", not "vendor string must be byte-equal"
  - The existing criterion is "vendor name + item description", this
    just turns the first half of the AND into a hard gate

**Rejected alternative:** add a post-hoc validator rule that checks
vendor match against the task's named vendor. Requires the validator to
know task semantics; skill guidance is the right layer.

## Not in scope

- **t041 / t066 (inbox position 16)**: documented as always-failing
  (0/5 historical). These need a dedicated investigation + design
  pass of their own. Skipping here avoids regression risk from
  half-understood changes.
- **R1 force-reject flip**: separate follow-up plan. Bench #1 showed
  zero R1 REJECTs under the new (case-insensitive + verified-absent)
  rule — see R4 correctness plan's Task 4 measurement. Flipping is
  still deferred until measured on a second trace.

## Acceptance

Bench #2 on the same `gpt-5.4 p10i15 prod runs1` configuration must
show server score ≥ 100 (no regression) and preferably ≥ 101. Single-run
variance is ±2 tasks, so a one-run bump is directional, not statistical.

## Follow-ups (NOT in this plan)

1. Dedicated inbox-position-16 investigation with trace inspection
   across ≥ 3 historical runs to identify the common failure mode
2. Flip `submit_anyway` → `force-reject` after bench #2 confirms R1
   continues producing zero REJECTs
3. Receipt-total-relative: consider a dedicated skill body for
   `receipt_*` intents (separate from `finance-lookup`) that
   hard-codes the vendor-match guardrail at step 1
