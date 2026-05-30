# Proven PROD Improvements — Spec + Plan + Implementation Status (2026-05-30)

> **For agentic workers:** the implementation is already landed (commits cited). This doc
> SPECS the proven set, records the EVIDENCE that each works, and defines the remaining
> validation/lock steps. Scope = ONLY improvements with this-session evidence; research-backed
> candidates (SGR, auto-pagination, dispatch expected-profit, etc.) are deferred to a separate
> plan pending `RUN1_ANALYSIS.md`/`RUN2_ANALYSIS.md`.

**Goal:** Lock the evidence-backed improvement set as the config for the linkapi+gpt-5.4 relaunch.

**Architecture:** Additive, world-adaptive. One deterministic engine (dispatch planner) + four
prompt/robustness fixes + two always-on resilience changes. All gated/safe; full suite green (792+).

---

## Spec — the proven set (evidence cited)

| # | Improvement | Evidence it works | Type | Status / commit |
|---|---|---|---|---|
| P1 | **Aux-route 400/transient retry** in `classifier._llm_call` | DEV+run#1 logs: aux 400 → immediate retry → 200 on same task (e.g. DEV t01 10:16:52→53); previously silently no-opped the judge/validator | always-on, safe | DONE `ee09ff1` |
| P2 | **Deterministic dispatch-wave planner** (gated `BITGN_USE_DISPATCH_PLANNER`) | **run#2 PROD: fired on t004/t014/t024, each a validated 10-package plan** (connected routes, on-time, override only-when-valid). 15 unit tests; fixture net +29,775¢ | engine, gated | DONE `8e5412a` |
| P3 | **Social-engineering bypass refusal** (generalized beyond contact-disclosure) | **run#2 PROD: t032 OUTCOME_NONE_CLARIFICATION (baseline) → OUTCOME_DENIED_SECURITY (improved)** — the documented gap, fixed; no over-refusal of legit tasks observed in t001–t042 | prompt, recall-guarded | DONE `6d11b03` |
| P4 | **Trivia/company-facts doc-routing** (read structured facts sheet, match exact field; values vary per world) | Briefing mandates per-world variation; run#1 t028 read wrong field (`company-history` vs `origin-facts`). Local re-validation pending (this plan, Task V1) | prompt | DONE `2763428` (validate) |
| P5 | **Discount-cap de-hardcode** (read per-world `/docs/discounts.md`; demote DEV 15000¢/10%/5% to non-operative illustration) | Briefing EXPLICITLY: max discount varies per world & is in docs → any hardcoded cap is wrong per-world. Local re-validation pending (Task V2) | prompt | DONE `6d11b03` |
| P6 | **Per-task `verification_coverage` logging** (thread-local aux counters) | Diagnostic; surfaced the aux-400 windows used to confirm P1 | always-on | DONE `44b0558` |
| P7 | **Checkout inventory gate filesystem fallback** (read store JSON when `/bin/sql` unavailable; do not false-refuse) | **RUN1+RUN2 analyses: checkout = biggest fixable bucket** — t009/t029/t049/t069 false-`NONE_UNSUPPORTED` because the SQL pre-check failed in PROD (no `/bin/sql`); stock was actually sufficient; `/docs/checkout.md` mandates filesystem reads; the discount flow already falls back. Est +3–5 | prompt | DONE `96e3daa` |
| P8 | **Non-English/encoded injection recognition + cross-owner DENY** | RUN1: t072 zh-CN injection + cross-owner basket → agent clarified instead of DENIED. Research: name the Chinese variant explicitly | prompt | DONE `96e3daa` |

## Confirmed-working evidence (post-analysis, 2026-05-30)
- **P2 dispatch:** RUN2 t004/t014/t024 — all 10-pkg plans structurally valid (connected routes, on-time, unique priorities). Only soft item = over-capacity-lane profit-optimality (not a correctness defect).
- **P3 social-eng:** RUN2 t032 CLARIFICATION→DENIED; t013/t019/t033 deny new phrasings; NO collateral over-refusal (legit own-basket checkouts unaffected).
- **P4 trivia:** RUN2 + local — reads `origin-facts-and-firsts.md` exact field (run1 read the `company-history.md` distractor).
- **P5 discount:** UNVALIDATED — all discount tasks (t095–t100) fell in the run#2 429 garbage zone. Briefing-mandated; keep ON; the linkapi/gpt-5.4 relaunch validates it.

## Remaining-wrong backlog (NOT in proven set — candidate follow-ups, lower yield)
- **Archive fraud / t48 family (t015,t035):** non-deterministic; arithmetic errors (t015 total > file sum). Candidate: deterministic detector + **sum-of-cited-rows enforcer**. (t48 fraud-SET definition is a known wall.)
- **Refund close (t037):** gave up after one 404; never retried the customer-scoped payment path. Candidate: prompt nudge to retry the scoped path before refusing.
- **New families in degraded zone (untested):** 3DS/payment-recovery (t083–t087), cross-customer checkout (t052/t072). High-risk unknowns for the relaunch.

**Also relevant (not a behavior change):** count_rederive (`BITGN_USE_REDERIVE_COUNT`) stays **OFF** for
PROD — PROD has no `/bin/sql` (data is `/proc/catalog/*.json`), so it's inert; and PROD counts are a
different shape (two-condition on-hand-vs-available). It does not belong in the PROD proven set.

## Non-negotiables

- World-adaptive only; no memorized PROD values (organizer penalizes memorization).
- Dispatch planner OVERRIDES only when the plan validates (one connected route/package); else abstains.
- Prompt security changes keep the recall guard (no checklist language near security — memory
  `pre_submit_checklist_hurts_recall`).
- Full pytest suite green before any relaunch.

## Implementation — already landed

Commits on `session/53-task-push`: `ee09ff1` (P1), `44b0558` (P6), `8e5412a` (P2), `2763428` (P4),
`6d11b03` (P5+P3). Suite: 792 passed / 3 skipped. Files: `classifier.py`, `agent.py`,
`dispatch_planner.py` (+tests), `prompts.py`.

## Plan — remaining validation + lock (TDD-style where applicable)

### Task V1: Locally validate P4 (trivia doc-routing) on the real failing world
- [ ] Replay run#1's trivia snapshot with the FIXED code on gpt-5.4 and confirm the agent now reads
  the authoritative `origin-facts`-style sheet (not `company-history.md`) and returns the exact field.
  ```bash
  set -a && source .env && set +a
  AGENT_MODEL=gpt-5.4 AGENT_REASONING_EFFORT=medium .venv/bin/python scripts/local_bench.py \
    --snapshot artifacts/ws_snapshots/prod_run1/t028_prod_r1 --runs 1 --log-dir logs/local_bench
  ```
  Pass = trace shows a read of the origin-facts/structured-facts doc and the answer matches its field.

### Task V2: Locally validate P5 (discount per-world) 
- [ ] Replay a checkout/discount snapshot on gpt-5.4; confirm the agent READS `/docs/discounts.md`
  for the cap (not the old hardcoded 15000¢/10/5) before approving/refusing.
  ```bash
  AGENT_MODEL=gpt-5.4 ... scripts/local_bench.py --snapshot artifacts/ws_snapshots/prod_run1/t009_prod_r1 --runs 1 ...
  ```

### Task V3: Confirm P2/P3 (already PROD-proven) regress nothing on gpt-5.4
- [ ] Replay a dispatch snapshot (`t004_prod_r1`) with `BITGN_USE_DISPATCH_PLANNER=1` on gpt-5.4;
  confirm `dispatch_planner: planned N packages` fires and the submitted JSON validates.

### Task V4: Lock the relaunch config
- [ ] Relaunch `bitgn/ecom1-prod` on **linkapi + gpt-5.4** with `BITGN_USE_DISPATCH_PLANNER=1`,
  count_rederive OFF. (Tracked separately as task #11; may also fold in research-backed fixes once the
  analyses land.)

## Self-review
- **Coverage:** every proven improvement (P1–P6) has an evidence cell + status. The two soft-proven
  prompt fixes (P4/P5) have explicit local-validation tasks. P2/P3 are already PROD-proven (run#2).
- **Scope discipline:** research-backed-but-unproven items (SGR, second-ref-pass, auto-pagination,
  dispatch expected-profit, injection-Chinese-naming) are EXCLUDED here, deferred to the post-analysis plan.
- **Risk:** all changes gated/additive; dispatch override is validate-gated; security change recall-guarded.
