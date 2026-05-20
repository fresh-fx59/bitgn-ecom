# Status — BitGN ECOM contest agent, v0.1.108 milestone

## Headline

**42/42 mean 1.000 on two consecutive PROD runs at v0.1.108.**
The deterministic floor is confirmed.

| Run | Result |
|---|---|
| v0.1.108 PROD #1 | 42/42 mean 1.000 |
| v0.1.108 PROD #2 (stability) | 42/42 mean 1.000 |

Session arc: 30/31 (v0.1.44 baseline) → 42/42 deterministic
(v0.1.108) across ~40 PROD iterations.

## Locked-in stack (don't touch unless a fresh failure mode appears)

### Post-pass enforcers (in order, agent.py `_post_process_terminal`)

1. **`refusal_cite_enforcer`** — DENIED_SECURITY ref classifier:
   strips contested action target unless an approval/delegation/
   coverage claim names it; strips PII-leaking employee/customer
   records; respects `actor_id` from pre-pass `/bin/id`.
2. **`refusal_message_scrubber`** — replaces non-actor `cust_NNN`
   / `emp_NNN` in refusal message text.
3. **`addenda_completer`** — sweeps `/docs/*` via tree (JSON parse)
   + `find` fallback; 4 filename prefixes (catalogue-count /
   counting / reporting / addenda) + bare reporting/counting;
   fuzzy slug match w/ singular/plural normalization; 3 task
   phrasings.
4. **`cite_completer`** — hardcoded action-family policy triples
   (checkout / discount / 3DS recovery).
5. **`sku_completer` (P1 count_per_store)** — uses `task_spec` to
   SQL-resolve qualifying SKUs per product (relaxation ladder:
   strict → brand+model → brand only) and union into refs.
6. **`sku_completer` (P1 yes_no_sku)** — enumerates brand+model
   family + brand+name fallback (no brand-only to avoid cross-
   category overshoot); union into refs.
7. **`sku_verifier`** — drops cited `/proc/catalog/*` whose
   `properties` contradict task spec.
8. **`fraud_recall_completer`** — adds canonical fraud cluster rows
   (multi-pattern SQL) the agent may have missed.
9. **`fraud_cluster_filter`** — drops single-device-customer
   payments from cited fraud set.

### LLM-side rules (prompts.py)

- Rule B count-cite parity (anti-overcite)
- D2 clarification enumeration (NONE_CLARIFICATION only)
- Delegation/coverage/issuer KEEP language
- Pre-checkout inventory gate (basket lines vs `available_today`)
- Actor-role gates the action (not approver's role)
- Refusal text MUST NOT name other persons by id
- Verbatim entity `status` word in message ("paid" vs "completed")
- **P1 task_spec REQUIRED on Shape A/B/C** (count_per_store /
  catalogue_count / yes_no_sku)
- yes_no_sku ENUMERATE-THEN-COMPARE workflow (3-step protocol)
- **count_per_store pre-submit per-product verdict self-check**

## What was tried and reverted

- High reasoning_effort (v0.1.93): higher variance, not lower.
- Naive count-override (v0.1.106): broke correct LLM answers when
  SQL relaxation dropped filters or store_descriptor was multi-
  store. Per saved memory
  `feedback_enforcer_cannot_replace_adaptive_llm`: enforcers
  should ADD refs, never rewrite the LLM's numeric answer.
- Standalone regex-based SKU completer (v0.1.84): natural-language
  parser too brittle vs PROD task surface.
- Brand-only yes_no_sku fallback (v0.1.103): cross-category
  overshoot.

## Local test infrastructure

- `tests/test_fraud_filter_t40_snapshot.py` — full SQLite mirror
  of t40_v155_fail. Fraud iterations cost $0.
- `tests/test_sku_completer.py` — real catalogue.db (15 tests).
- `tests/test_addenda_completer.py` — 19 tests (4 prefixes, 3
  phrasings, JSON tree, fuzzy slug, sing/plural).
- `tests/test_refusal_cite_enforcer.py` — 33 tests across all
  refusal-cite families.
- Local harness aligned to PROD (v0.1.97): 16 KiB
  max_tool_result_bytes, tree() raises on missing path, find
  accepts `paths` and `matches` shapes.

## Cost

~$700 PROD bench credits across ~40 runs. Most ROI came from:
- v0.1.78 fraud filter parser fix (t40 → 1.0)
- v0.1.96 addenda find() fallback (closed t12 family)
- v0.1.108 count self-check (closed t13 family)

## Next session entry

The v0.1.108 stack is the new baseline. If a fresh PROD run drops
below 42/42, trace the specific failing task and:
1. Look for the agent's `task_spec` shape — did it classify
   correctly?
2. Look for completer events in the trace
3. If a deterministic root cause exists, ship a targeted fix
4. If it's LLM-side reasoning, consider strengthening the
   relevant prompt rule

Don't touch the stack speculatively. Each removal/loosening risks
the deterministic floor.
