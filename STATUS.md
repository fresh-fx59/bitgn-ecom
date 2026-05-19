# Status — BitGN ECOM contest agent, end of v0.1.84 session

Last bench (PROD, single-run): **v0.1.84, 41/42 (97.6%), mean 0.976.**
Two consecutive same-config runs (v178, v179a, v179b, v184) confirm
this is the deterministic floor; variance picks 1 of ~7 tasks per
run.

## Session arc

```
ver     pass  mean   notable change
v0.1.44  30/31 0.967 (31-task baseline)
v0.1.47  34/40 0.852 (40-task contest)
v0.1.49  38/40 0.987 fraud multi-pattern enumeration
v0.1.56  40/42 0.974 approval-claim + doc-triple + fraud second-pass
v0.1.60  39/42 0.951 prompt + enforcer iterations land
v0.1.61  39/42 0.951 rule B count-cite parity (t13 fix)
v0.1.65  38/42 0.927 sku_verifier
v0.1.66  40/42 0.974 enforcer delegation phrases
v0.1.69  40/42 0.974 cite_completer (action triples)
v0.1.74  40/42 0.974 fraud filter — id-share AND time-cluster
v0.1.75  41/42 0.989 fraud filter — distinct-device discriminator (t40 still 0.927)
v0.1.77  41/42 0.998 best mean (t40 = 0.927 due to parser bug)
v0.1.78  41/42 0.976 fraud filter parser fix — t40 = 1.0 (real fix!)
v0.1.79  41/42 0.976 enforcer cross-customer cite strip
v0.1.81  40/42 0.952 actor_id from pre-pass /bin/id plumbed through
v0.1.82  40/42 0.952 sku_completer (regex-based)
v0.1.83  38/42 0.905 addenda_completer + apostrophe contractions
v0.1.84  41/42 0.976 completers disabled (net-negative)
```

## What's in the proven stack (v0.1.84)

Per `memory/project_ecom_v184_final_stack.md`.

- **`fraud_cluster_filter.py`** — t40 deterministic 1.0.
- **`sku_verifier.py`** — drops wrong-attribute SKUs.
- **`cite_completer.py`** — hardcoded action-family policy triples.
- **`refusal_cite_enforcer.py`** — cross-customer strip with
  actor_id, delegation/coverage KEEP, approval-claim KEEP, PII
  strip, apostrophe-contraction role-policy detection.
- **`prompts.py`** — rule B count parity, D2 clarification
  enumeration, delegation triggers, pre-checkout inventory gate.

## What's built and disabled (not in agent.py post-process)

- **`sku_completer.py`** + tests — natural-language regex parser
  too brittle. PROD measured net-negative.
- **`addenda_completer.py`** + tests — same problem.

Both stay in tree pending a structured-spec input (see SPEC_NEXT.md
P1).

## Local test fixtures available

- `tests/test_fraud_filter_t40_snapshot.py` — full end-to-end
  against the t40_v155_fail snapshot's 25 payment JSONs +
  synthetic SQLite. Verifies fraud filter drops exactly the 3
  cust_025 single-device FPs.
- `tests/test_sku_completer.py` — uses the real
  multi_sku_attr_line_hard catalogue.db. 10 tests.
- `tests/test_addenda_completer.py` — 9 tests with fake tree mock.
- `tests/test_refusal_cite_enforcer.py` — 30 tests, all
  refusal-cite shapes.

## Remaining 1/42 failure families (in priority order)

1. **SKU recall on multi-line count tasks** (t08, t14, t15, t16)
   — agent searches wrong catalogue partition for a multi-product
   list. Fix path: SPEC_NEXT P1 + P2 (structured TaskSpec from
   the LLM + robust SQL).

2. **Multi-addenda discovery** (t12) — agent reads 1 of N matching
   catalogue-count addenda. Fix path: SPEC_NEXT P3 (re-enable
   addenda_completer driven by TaskSpec).

3. **Wrong-outcome on actor-role refusal** (t28) — agent applies
   role-gated action with cust role because manager has the role.
   Fix path: SPEC_NEXT P4 (prompt rule).

4. **Message-text customer leakage** (t34 v183 form) — refusal
   names other customer by id in message body. Fix path: SPEC_NEXT
   P5 (prompt rule + post-pass scrubber).

## What was tried and didn't stick

- **`sku_completer` / `addenda_completer` (regex)** — disabled in
  v0.1.84. Re-enable once structured TaskSpec lands.
- **Single-pattern fraud filter** (v0.1.70) — too lenient, didn't
  drop FPs.
- **Multi-pattern fraud filter with `min_patterns >= 2`** (v0.1.72)
  — too lenient too; the 3 FPs satisfied 2+ patterns.
- **Identity-share AND time-cluster AND-gate** (v0.1.74) — all 25
  cited rows satisfy both; couldn't discriminate.
- **All-time `cust_device_count >= 2` SQL** (v0.1.75 pre-scope) —
  PROD inflated count because cust_025 has prior all-time payments
  at different devices.
- **Pipe-separated SQL parser** (v0.1.70 - v0.1.77) — `/bin/sql`
  returns CSV in JSON envelope, not pipes. Caught by debug-output
  bench in v0.1.78.
- **Pre-submit checklists in prompts** — measured net-negative on
  recall-bound tasks (memory: feedback_pre_submit_checklist_hurts_recall).
- **Fixed-probe preflight enforcer** (v0.1.52) — lost to LLM's
  adaptive query strategy (memory: feedback_enforcer_cannot_replace_adaptive_llm).
- **Pre-submit checklist** (v0.1.50) — hurt recall.

## Cost of session

~$300 in PROD bench credits over ~25 PROD runs. The user flagged
this as a process gap; future iterations should use the local
fixtures first. The fraud filter alone burned ~$60 across 6 PROD
runs before the parser bug + schema bug were caught — those would
have been free locally.

## Next session entry point

Start at `SPEC_NEXT.md`. Implement in the order P3 → P5A → P4 →
P1 → P2 → P5B. Local test every step; PROD validate at the end of
each landed module. Target: 42/42 mean 1.0 on a single PROD run,
then 41+/42 mean ≥0.98 on a follow-up to characterize variance.
