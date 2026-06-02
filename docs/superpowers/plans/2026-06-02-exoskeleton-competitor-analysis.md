# Competitor analysis: muxx/bitgn-ecom1-exoskeleton (2026-06-02)

Deep dive into the **1st-place (Speed + Live PROD)** ECOM1 solution
(`@dev_salikhov ecom1 gpt-5.4-mini`), repo cloned to /tmp/exo. Goal: extract
anything that moves our PROD score past the ~0.89 band.

## Headline findings

1. **The ceiling is real — confirmed by an independent winner.** Their score
   dashboard (articles/images/score_dashboard.png) is the same mostly-green-with-
   scattered-red "blinking" heatmap in the **~85–90 band**. They did NOT reach 100
   either. Their `dispatch_planner.py` has NO efficiency loophole (treats it as a
   routing problem like ours; dispatch tasks sit partial on their heatmap too).
   → corroborates [[project_ecom_dispatch_ceiling_proof]].

2. **Architecture ≈ ours.** "Exoskeleton" = deterministic harness around a model
   dispatcher: parallel native tool-calls (`tool_choice=required`,
   `parallel_tool_calls`), synthetic start context, deterministic PREFLIGHT checks
   that close tasks before the model, domain HELPERS (catalog/fraud/3DS/dispatch),
   an EVIDENCE LEDGER, REF NORMALIZATION, a FORMATTER. We have analogues of all of
   these. Validates our design.

3. **They won on a SMALLER model (gpt-5.4-mini main, gpt-5.4-nano aux).** The
   harness, not the model, carries the score. Implication: model choice is not our
   bottleneck; the deterministic layers are.

## Fraud — TESTED their detector live, NOT a win (same wall)

Ported their detector transiently (verbatim `fraud_rules.py` + a pure copy of
`archive_fraud.py`) into a probe and ran it against the LIVE grader on
t015/35/55/75 over 3 fresh instances each. NOTE: the ported code + probe were
REMOVED from the tree (the competitor repo carries no LICENSE — don't vendor it);
the result data is retained at artifacts/local_bench/fraud_exo_probe.json and the
verdict below stands. To reproduce, re-clone muxx/bitgn-ecom1-exoskeleton:

| task | r1 | r2 | r3 | mean |
|---|---|---|---|---|
| t015 | 0.871 | 1.00 | 0.564 | 0.81 |
| t035 | 0.963 | 1.00 | 0.912 | 0.96 |
| t055 | 0.937 | 0.954 | 0.529 | 0.81 |
| t075 | 0.527 | 0.959 | 0.201 | 0.56 |

**Mean ≈ 0.785 over 12 instances** = same as our agent (~0.78), still wildly
instance-variant (0.20→1.0). Their rule = impossible-travel city-hops in time
windows + rapid-burst/high-value tables + the key precision trick
`CUSTOMER_CONTROLLED_CHANNELS` (device fingerprint authoritative only on
customer_terminal/mobile_app/web; kiosks/staff terminals excluded). It's a
different precision/recall operating point than our compNT, NOT a structural win.
Run-1 alone (0.825) would have misled us — the 3-run validation confirms fraud is a
genuine wall for BOTH solutions. NOTE: the archive TSV has `created_at` +
`archive_channel` columns our old fraud_probe ignored. → updates
[[project_ecom_fraud_archive_structure]].

## Grounding-refs — THE transferable value (deterministic, low-variance, exact-set graded)

submission_refs.py is far more complete than our ref_judge/cart_ref_judge/
count_ref_completer. Rules that map to OUR specific flippers:

| Their rule | Fixes our flipper | Risk |
|---|---|---|
| `filter_crosslist_refs` — crosslist/export task → cite ONLY the `/uploads/` OCR ref | **t076 "too many invalid references"** | low (narrow) |
| discount task + employee actor → add `employee_ref_by_id(user_id)` | **t099 "missing /proc/staff/.../emp ref"** | low |
| `normalize_submission_refs`/`canonical_proc_record_ref` — stat-check EVERY ref, rebuild from id, DROP non-existent | invalid-ref failures generally | low (only drops dead paths) |
| `message_sku_refs` — auto-cite every SKU surfaced in the final message | surfaced-SKU undercite | med (exact-set: could add an extra) |
| `align_count_catalog_refs_to_answer` — answer=N → keep only first N catalog refs | t047 count-cite parity | med |
| `linked_payment_refs_for_returns` — refund → also cite the reversed payment | refund tasks | low |
| `replace_customer_facing_employee_refs` — customer/guest ctx → swap emp profile for store ref (privacy) | leak-prevention | low |

All deterministic + ownership-gated. Refs are graded as an EXACT set with low
per-instance variance, so pinning them is higher-ROI than fraud/dispatch.

## Other techniques worth noting (already have analogues)

- **Formatter on nano** (two-pass: does the task need a format? does the answer
  match?) — addresses `<COUNT:1>` vs "we have one", stray `OUTCOME_*` prefixes. We
  have answer formatting; theirs is a clean dedicated component.
- **Security = blast radius** (classifier intent + `/bin/id`, decide by what the
  task DOES, not word blocklists). We have v0.1.170 recalibration; same principle.
- **Catalog helper** = parse-to-constraints + match-ALL exact + numeric-strict +
  negated-inverts + prefer-no/clarify. Same as our sku resolution.

## Recommended adoption (prioritized, deterministic, probe-validatable for free)

1. **Crosslist ref filter (t076)** — cleanest, narrowest, lowest-risk. Cite only
   the task's `/uploads/` OCR ref on crosslist/export tasks.
2. **Discount → issuer-employee ref (t099)** — add the actor emp record when the
   actor is an employee and the task applies a discount.
3. **Stat-check-and-drop invalid refs** — general hygiene; drop any cited ref that
   doesn't resolve via runtime stat.

Validate each via scripts/run_filtered_bench.py (synchronous grader scores, ~free)
before any full run. Expected: pins ~2–3 flippers deterministically (refs are
low-variance) → best single run ~89→~92 combined with best-of-N. Does NOT change
the structural ceiling (~99.4) or the fraud/dispatch walls.

## SHIPPED v0.1.171 — three exo ref rules (src/bitgn_contest_agent/exo_ref_rules.py)

Implemented all three as env-gated pure functions (16 tests, suite 954✓/3 skip),
wired LAST in the terminal enforcer chain (stat-guard absolutely last):
- `BITGN_USE_CROSSLIST_REF_FILTER` — crosslist task → cite ONLY /uploads/ OCR ref
- `BITGN_USE_DISCOUNT_EMP_REF` — discount + employee actor → add actor's staff record
- `BITGN_USE_REF_STAT_GUARD` — drop /proc record refs the runtime confirms missing
  (keeps seen refs, docs, /archive #row, and unknown/transient — conservative)

**Probe validation (run-22SUyt, filtered_bench, gpt-5.4, all 3 flags ON):**
t076=1.0 (crosslist fix landed — was "too many invalid references"), t016=1.0
(2nd crosslist), t099=1.0 (discount→emp fix — was "missing emp record"), t095=1.0
+ t005=1.0 (controls, NO regression), t002=0.0 (pre-existing but_not, unrelated).
2 target fixes landed, 0 regressions. Single-instance each (flippers) → confirm on
the full run. Then full PROD run run-22SV4N (v0.1.171, same flags).
