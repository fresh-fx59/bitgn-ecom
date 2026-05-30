# Spec — In-trial answer re-derivation for reliable 53/53 (2026-05-30)

> Read `docs/FINDINGS_RELIABILITY_2026-05-30.md` first. This spec defines WHAT to
> build; `docs/superpowers/plans/2026-05-30-answer-rederivation-verifier.md`
> defines HOW, task-by-task.

## Goal

Turn the variance-prone families from *sometimes-right* into *reliably-correct*
by giving the agent an independent, deterministic, in-trial re-derivation of its
own answer that **bounces** the terminal back into the loop on disagreement —
reaching a stable ~52/53 (t48 is a separate empirical probe).

## Design principles (non-negotiable — each encodes a proven failure)

1. **Re-derive through a DIFFERENT path, don't resample the same one.** (Voting
   failed because it resampled the biased reasoning.)
2. **BOUNCE, don't silently rewrite.** On disagreement, reject the terminal with
   the diff + the convention spelled out, so the adaptive model reconciles. Never
   overwrite the model's number. (Silent override = v0.1.106 regression +
   `feedback_enforcer_cannot_replace_adaptive_llm`.)
3. **DETERMINISTIC: pure `/bin/sql` + Python, no aux-LLM call.** (The Haiku aux
   route 400-blacks-out 0/53 in 3 of 5 runs — any LLM-dependent verifier is
   randomly absent. See `project_ecom_provider_400_blackout`.)
4. **ABSTAIN on genuine ambiguity.** If a product can't be resolved to one
   threshold side, or no dominant fraud seed exists, do nothing — the agent's
   answer stands. Worst case == today. (Abstaining is always safe; a wrong bounce
   only costs one extra agent turn.)
5. **Use the contest's own conventions, not memorized answers.** Read
   `/AGENTS.MD`, addendum bodies, `/docs/*.md`; resolve via the live catalogue.
   No hardcoded SKUs/stores/counts → not overfitting.
6. **Validate locally against the `*_real2` oracle DBs at ~$0 before any PROD
   run.** The prior overrides were validated against false-passing snapshots; the
   `*_real2` snapshots carry ground-truth `expected_answer`.

## Conventions the re-derivation enforces (from the workspace, proven)

- **count_per_store / catalogue_count availability:** `available_today =
  COALESCE(store_inventory.available_today_quantity, 0)`. A SKU with no inventory
  row at the named store has **0 available**. Apply the parsed threshold predicate
  verbatim; it is **direction-dependent** (`<N`/`no availability` → 0 qualifies;
  `≥N`/`at least` → 0 excluded). Unit of the count is the **listed product**,
  counted once if its resolved canonical variant(s) satisfy the predicate.
- **catalogue_count filter:** if the matched addendum doc body contains an
  availability/store predicate (`available_today`, `available today`, `open …
  store`, `in <City>`, `>0`), the count is the **inventory+store-joined distinct
  SKU count**, not the raw `product_kind_id` count.
- **variant/SKU resolution from a model code:** always `model LIKE '%CODE%' OR
  product_name LIKE '%CODE%'` (never `model = 'CODE'`). Attributes matched
  unit-insensitively against `product_variant_properties` using BOTH
  `property_value_text` and `property_value_number`.
- **fraud (SQL t40/t39):** the answer is the connected component of archived
  payments under "shares `device_fingerprint` OR `payment_method_fingerprint`",
  seeded from the dominant anomalous fingerprint (top archived count ≥15 AND ≥2×
  runner-up). Gate strictly to the fraud-incident task text (`fraud`-word +
  `payment`/`archiv`-word, NOT `.tsv`) — the same anomalous cluster is seeded in
  every world's background DB, so the gate (not the math) is the precision boundary.
- **checkout/selection:** "the one I started most recently" = the unique
  `max(basket_created_at)` among `basket_status='active'` baskets; gate that
  basket alone; no fall-through to an older basket.

## Components (each independently shippable + A/B-able)

| Phase | Module | Responsibility | Local oracle |
|---|---|---|---|
| 0 | `classifier.py`, `arch_log` | Retry 400s on the aux route; log `verification_coverage`. Removes a variance source; makes benches honest. | n/a (unit + run-log grep) |
| 1 | `count_rederive.py` (new) | Pure-SQL re-derivation of count_per_store; returns count or ABSTAIN + per-product verdicts. Bounce on disagreement. | `t45_real2`→4, `t16_real2`→3 |
| 2 | `catalogue_count_rederive.py` (new) | Read addendum body; if it states the availability predicate, require the inventory-joined count. | t11/t49 family worlds |
| 3 | `quote_rederive.py` (new) | All-rows-empty guard → `model LIKE` re-resolution; "my store" triple-check. | `t47_real2`→4 SKUs |
| 4 | `agent.py`, `sku_completer.py` | Disable `yes_no_sku_completer` flood; replace with SQL self-verify → confirmed singleton refs. | `t01_real2`→[STO-2R84BSHQ] |
| 5 | `agent.py`, `fraud_component_completer.py` | Run the device∪method closure in-trial (gated); reconcile/adopt the component. | `t40_real2`/`t40_real3`→26 rows |
| 6 | `basket_select_rederive.py` (new) | t50 no-fall-through assertion; t26 reconciliation aggregate. | `t26_real3`→basket_053 |
| 7 | `t48` probe (PROD) | Read-to-EOF reliability + cross-customer-ring hypothesis; empirical PROD A/B + `fetch_trial_detail`. | none (ground truth unknown) |

## Integration contract

- The re-derivation runs in `AgentLoop._post_process_terminal` /
  `run()` (it needs `self._adapter` for `/bin/sql`). `run_sql` is built exactly
  like the existing one at `agent.py:1620-1629`:
  ```python
  from bitgn_contest_agent.adapter.ecom import Req_Exec
  def _run_sql(sql: str) -> str | None:
      try:
          tr = self._adapter.dispatch(Req_Exec(tool="exec", path="/bin/sql", args=[], stdin=sql))
          return tr.content if tr.ok else None
      except Exception:
          return None
  ```
  `/bin/sql` returns CSV (header row + comma-separated); parse with
  `sku_completer._unwrap_sql` and `_csv_split`.
- **Bounce mechanism:** `StepValidator.check_terminal` returns `Verdict(ok: bool,
  reasons: List[str])` (`validator.py:45-48,116`). A disagreement is surfaced by
  causing the terminal handling in `run()` to treat the step like
  `verdict.ok == False` with the re-derivation's reason appended — the existing
  retry path (`agent.py:498-630`) re-injects the reasons as a correction and lets
  the model resubmit. Cap re-derivation bounces at **1 per task** (after one
  retry, accept the model's answer — abstain-to-agent — to avoid loops).
- Everything is **env-gated default-off** during development
  (`BITGN_USE_REDERIVE_COUNT=1`, etc.) so each phase A/Bs against the proven
  baseline. Flip to default-on per phase only after a clean local + PROD A/B.

## Acceptance criteria

- **Local (per phase, mandatory before PROD):** the re-derivation reproduces the
  oracle answer on every `*_real2` snapshot for its family, bounces every
  documented failing trace to the correct answer, and is a no-op on every passing
  trace. Full `pytest` suite stays green.
- **PROD (per phase):** one DEV A/B (phase ON vs proven baseline). The phase ships
  default-on only if it does not regress and the targeted family improves.
- **Overall target:** a *tight* band ~50–52/53 (not 38–51), with t40/t39 at 1.0.
  t48 tracked separately.

## Out of scope / explicit non-goals

- No new LLM-as-judge layer (provider-flaky). No voting. No silent count override.
- No `contest_profile.py` refactor (parked — `docs/OVERFITTING_AUDIT.md`).
- t48's fraud-set definition is NOT promised solvable; Phase 7 is a probe.
</content>
