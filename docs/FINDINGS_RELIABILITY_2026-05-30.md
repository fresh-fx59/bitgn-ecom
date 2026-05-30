# Findings — the ECOM "seed variance" is agent unreliability, and it's fixable (2026-05-30)

> Source: a 6-agent investigation workflow (540k tokens, full per-family trace
> analysis) + independent oracle re-derivation by the lead. Raw structured
> output archived inline below. This doc is self-contained; it does not depend
> on the workflow temp file surviving.

## TL;DR

The 38–51/53 PROD swing the prior sessions labeled "irreducible seed variance"
is **agent unreliability on a small set of systematic, per-world interpretation
forks** — not noise on a fixed seed. Proof:

1. Local benches draw a **different world every run** (store, products, threshold
   value *and direction* all change run-to-run). So the swing is one *unreliable
   method* applied to fresh content, not sampling jitter on one world.
2. Over 12 PROD runs, **every task except t40/t48 hit 1.0 at least once.** The
   agent demonstrably *can* do each task; it just doesn't do it *reliably*.
3. The flips are **systematic** (the agent leans the wrong way >50% of the time
   on the failing worlds), which is exactly why the two prior remedies failed:
   - **Voting** resamples the *same biased reasoning* → wrong majority (t45 ~89%
     wrong → K=3 majority also wrong). 43–48/53, no lift.
   - **Deterministic overrides** did the *semantic* attribute match with a rigid
     string matcher (`_phrase_satisfied`) and abstained whenever 1 of 6 products
     didn't string-match → "fired nowhere." 45/53.

The missing capability is the one the user named: **the agent catching its own
variance** — an independent in-trial *re-derivation* of its answer through a
different path that reconciles on disagreement. Neither voting nor a rigid
override is that.

## The core convention proof (t45) — settles a contradiction

Two investigators disagreed on the make-or-break fact. Resolved by running the
oracle DB directly (`artifacts/ws_snapshots/t45_real2/run_0/workspace/catalogue.db`,
expected `<COUNT:4>`, "fewer than 4"):

| SKU @ store_innsbruck_wilten | inventory row? | available_today | `<4` if missing→0 | `<4` if missing→excluded |
|---|---|---|---|---|
| GRD-138CLG7H | no  | 0 | ✅ | ❌ |
| AUT-3TE8KXP4 | yes | 5 | ❌ | ❌ |
| STO-3GODQ8BL | no  | 0 | ✅ | ❌ |
| WRK-18W93PML | yes | 9 | ❌ | ❌ |
| ADH-2D3Q64KH | yes | 2 | ✅ | ✅ |
| AUT-3UP372ID | no  | 0 | ✅ | ❌ |
| **count** | | | **4 (= oracle)** | **1 (wrong)** |

**Convention (bedrock):** `available_today = COALESCE(store_inventory.available_today_quantity, 0)`;
apply the threshold predicate verbatim; a product with **no inventory row at the
store has 0 available**. It is **direction-dependent**: for `<N`/`no availability`
a 0 *qualifies*; for `≥N`/`at least` a 0 is *excluded*. This is the fork the
agent improvises every run. The `/AGENTS.MD` "rule C" (`prompts.py:291-313`) is
about **which stores to cite as refs**, NOT count semantics — do not conflate the
two (that was the contradicting investigator's error).

## Per-family root cause + reliable fix

| Tasks | Failure mode | Root cause (evidence) | Reliable fix | Confidence |
|---|---|---|---|---|
| **t13 t14 t16 t45** count_per_store | attribute_resolution / interpretation_fork | INNER-vs-LEFT join on `store_inventory` (a no-row SKU silently vanishes) + direction-blind missing-row handling + variant resolution. Failing run `logs/20260529_202603/t45` uses `INNER JOIN store_inventory` → count 1; passing run `…231603` uses LEFT JOIN → counts the 0s. | Convention above + **two-path re-derivation**: candidate set from `product_variants(+properties)` BEFORE inventory; LEFT JOIN+COALESCE; direction-aware predicate; **bounce** on disagreement. | **High** (proven t45→4, t16→3) |
| **t11 t49** catalogue_count | interpretation_fork | Agent drops the addendum BODY's availability+store filter and ships raw `COUNT(*)` (work_gloves=26, helmets=14) vs the doc-mandated inventory-joined count. `logs/…153217/t11` literally says "use normal count by product_kind_id" after reading the addendum. | Read addendum **body** (not filename); run raw AND inventory-joined; if body states the availability/store predicate, answer MUST equal the joined count. | **High** |
| **t47** quote/pasted-list | attribute_resolution | `WHERE model='CODE'` returns 0 rows for EVERY row (0/1000 variants store a bare 6-char code as `model`; all have full-string model) → all-empty output. `logs/…153217/t47` ships 7 empty rows; `…231603/t47` self-corrects to `model LIKE '%code%'`. | `model LIKE '%CODE%' OR product_name LIKE '%CODE%'`; **all-rows-empty sanity guard** forces re-resolution; triple-check "my store" (SQL `employee_accounts.store_id` == `/proc/employees/<actor>.json` == `/bin/id`). | **High** |
| **t01 t06 t07** yes_no_sku | missing_refs | **Agent verdict + SKU pick is ALWAYS correct** across 11 runs. The 0.0 is caused by the team's OWN `yes_no_sku_completer` (`sku_completer.py:917`) flooding never-read family refs → validator R1 reject ("never successfully read"); `sku_verifier` can't strip numeric-renamed attrs (`diameter`→`diameter_mm` NUMBER). | **REMOVE the flood completer**; SQL-self-verify the cited SKU via `product_variant_properties` (has text+number forms); set `grounding_refs` to the confirmed singleton already in `seen_refs`; abstain/prune only, never inject unread refs. | **High** |
| **t40 t39** fraud (SQL) | graph_closure | Device-only clustering misses method-linked ring members → stuck at 0.927 (=24/26). Agent's detection SQL uses `COUNT(DISTINCT customer_id)>=3` patterns that match ZERO rows on the single-customer (cust_025) oracle. | Run the **device∪method connected-component closure** in-trial (already coded in `fraud_component_completer.py`, returns exactly 26 on t40_real2/real3), gated on fraud-task text; reconcile/adopt the component. | **High** (converts 23 PROD runs of 0.927→1.0) |
| **t26** discount | interpretation_fork | Over-refusal at subtotal/availability boundaries + missing `/docs/checkout.md` ref. PROD `f0cfcfe`/`d506d06` returned NONE_UNSUPPORTED where OK was wanted. | One reconciliation aggregate query (group baskets, compute `all_lines_ok` + subtotal in one shot; assert chosen = first all_lines_ok by recency; recompute cap; assert full ref set incl `/docs/checkout.md`). | **Med-High** |
| **t50** checkout | interpretation_fork | **Fall-through bug**: when the newest active basket is unavailable, the agent checks out an OLDER ready basket. Grader wants the UNIQUE newest, else `NONE_UNSUPPORTED` (STATUS.md:88 required basket_123, rejected agent's basket_037). | Split selection from gating: pick the UNIQUE `max(created_at)` active basket in one query; gate THAT basket alone; **structurally forbid** checking out any basket id != the unique-newest. | **High** |
| **t48** archive fraud (TSV) | true_ambiguity | **No in-workspace rule defines the fraud set.** 162 rows / 68 customers; card-sharing=0; device-sharing(>1 cust)=12 in 2 rings; time-impossible=41 (some legit); coord=45 noisy. Agent answers swing 52/16/0/36 rows. PROD max ≈0.305. Also TSV read-truncation → computes on partial data. | Read-truncation half is mechanically fixable (enumerate rows to EOF, assert count). Fraud-set half is a **genuine wall** — one untested hypothesis (cross-customer device/method rings) must be probed on PROD. | **Low** |

## The architectural gap

Four "verification" layers exist; **none re-derives the answer**:

- `StepValidator.check_terminal` (`validator.py:116-239`): R0 min-explore, R1 ref
  reachability, R3 leaning/outcome, R4 mutation, R5-R7 attachments. Never
  recomputes a count. Emitted `TERMINAL ACCEPT` on a `COUNT:3` whose truth is 4.
- `VALIDATOR_T2` triggers (`validator.py:319-577`): premature-commitment /
  progress nudges. On t45 the first_transition trigger fired and the retry
  **flipped `COUNT:3→0→3`** — it *amplifies* variance on count tasks.
- `judge_enforcer` (`judge_enforcer.py`): RULE 2 = "NEVER rewrite the numeric
  answer." Only adds/drops refs; its RULE 5 even *drops* SKU refs on counts.
- "count self-check": a **prompt rule** run by the same biased model. No
  orthogonal recomputation.

## Provider reliability injects variance (must fix)

The aux-LLM layer (judge, validator T2, canonicalizer) runs on `claude-haiku-4-5`
via `api.linkapi.ai` and returns HTTP 400 intermittently; `classifier.py` does
NOT retry it (`max_retries=0`, retries only `JSONDecodeError`). Each 400 silently
no-ops one verifier. Per-run 400 counts (53 task logs each):
`231603`=37, `202603`=25, **`183918`=98, `153217`=61, `090326`=53** — i.e. **3 of
5 sampled runs had a TOTAL judge blackout (0/53 verified)**. So existing
verification is *randomly present or absent* — an independent variance source.
**Implication: the new re-derivation verifier MUST be deterministic (pure
`/bin/sql`+Python, no LLM call)** so it can't be no-opped by a 400.

## Why the fix is not overfitting

It encodes the contest's **own documented conventions** (read from `/AGENTS.MD`,
addendum bodies, `/docs/checkout.md`) and uses its **own tools** (`/bin/sql`) — no
memorized SKUs, stores, or answers. The division of labor — **LLM for per-world
semantic parse + atomic judgments, engine for enumeration/closure/arithmetic** —
is a domain-agnostic agent-design principle. It is the agent doing what a careful
human does: compute, independently re-check via a second method, reconcile.

## Honest ceiling

The design reliably converts everything except t48 to 1.0 → **~52/53
deterministically** (not on a lucky seed), and t40/t39 from 0.927→1.0. **t48 is
the one genuine wall**; whether 53/53 (pass@1.0) is even reachable comes down to a
single empirical PROD probe of the cross-customer-ring hypothesis (see
`docs/superpowers/plans/2026-05-30-answer-rederivation-verifier.md`, Phase 7).

## Key evidence pointers

- Oracle snapshots (faithful PROD schema, known answers): `artifacts/ws_snapshots/`
  `t45_real2` (→4), `t16_real2` (→"result 3"), `t40_real2`/`t40_real3` (→26-row
  component), `t47_real2` (→4 SKUs PWR-1ALYVIXX/PNT-1CJQ9WWP/FST-1GZ71C60/CLN-3066WM0S),
  `t26_real3` (→basket_053, OUTCOME_OK).
- Full multi-world local traces: `logs/20260529_{231603,224311,202603,195033,153217,134709,125515,113838,090326}/tNN__run0.jsonl`.
- Deterministic count convention encoded (but as a dead override): `src/bitgn_contest_agent/refless_count_override.py:194-213,276-317`.
- Fraud closure (correct, default-off): `src/bitgn_contest_agent/fraud_component_completer.py:103-147`.
- Memories: `project_variance_is_reliability_not_seed`, `project_ecom_53_path_per_family`,
  `project_ecom_t48_genuine_wall`, `project_ecom_provider_400_blackout`.
</content>
