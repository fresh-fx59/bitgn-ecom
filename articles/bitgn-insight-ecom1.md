---
source_code: https://github.com/fresh-fx59/bitgn-ecom
run_ids:
  - run-22Ry8RuZK1bonHd8eWqdNNKqc
model_names:
  - gpt-5.4
  - gpt-4.1-mini
author: Aleksei Aksenov
author_github: https://github.com/fresh-fx59
author_telegram: ai_engineer_helper
impact: The LLM is a dispatcher inside a deterministic harness — recurring error classes move out of the prompt and into code that re-derives the answer, the exact grounding-reference set, and the format around one expensive model loop.
challenge: ecom
---

# A model-dispatcher inside a deterministic harness

This is an agent for **BitGN ECOM1**. It began as a fork of the official PAC1
reference implementation (`bitgn/sample-agents/pac1-py`, which reached 104/104 on
its own PROD surface) and was ported to the ECOM1 runtime. The submitted
blind-window run on `bitgn/ecom1-prod` ([run-22Ry8RuZK1bonHd8eWqdNNKqc](https://eu.bitgn.com/runs/run-22Ry8RuZK1bonHd8eWqdNNKqc),
100 trials) scored **80.3** (mean 0.803) with `gpt-5.4`.

The core idea in plain English: **the model reasons and chooses tools; code does
everything that can be computed exactly.** Security consequences, fraud clusters,
store counts, the exact set of grounding references, and the answer format are
all handled by deterministic layers that bracket a single expensive model loop —
before it starts and after it speaks.

## How does it work?

A task is one trial against a Unix-like retail back-office (`/proc/...` record
trees, `/bin/sql`, `/bin/jq`, `/bin/id`, `/archive` exports, `/docs` policies).
The agent runs a fixed pipeline per task:

1. **Classifier + router** (cheap aux model) — extracts intent flags and entities
   (checkout, basket id, actor, impersonation attempt, …) and routes to a skill.
   Tier-1 regex on the original *and* English-normalized text; tier-2 aux
   classifier on a miss. Degrades gracefully if the aux provider is down.
2. **Deterministic preflight** — surfaces evidence before the model spends a
   token. The most consequential is a 4-probe fraud pass (shared card / shared
   device / time-impossible cross-store pairs / coordinate cluster) whose union
   is handed to the model as an observation, plus actor-identity extraction from
   `/bin/id`. The starting context is seeded as synthetic *tool-call/result
   pairs* so the model reads its own "prior actions" and continues naturally.
3. **Main loop** — the one expensive reasoning model. Native tool-calling
   (`tool_choice="required"`), parallel read batching when the primary action is
   read-only, a 40-step budget, and a salvage/chat fallback path for providers
   that mangle native tool calls.
4. **Terminal enforcer chain** — when the model reports completion, the answer
   passes through ~20 ordered enforcement steps before submission (see *Acting*
   and *Solutions* below).
5. **Validator + formatter** — deterministic rule gates, a re-derivation check,
   and a structured-output formatter that fixes the exact-format contract without
   re-solving. One optional re-verification call, hard-capped at 1/task.

The agent decides a task is finished when the model emits `report_completion`
with an outcome and its grounding references — which the harness then completes,
verifies, and reconciles against the live world.

## Models

- **Main solver:** `gpt-5.4`, reasoning effort `medium` (empirically
  lower-variance than `high`). This is the only place expensive reasoning happens.
- **Classifier / router / judges / formatter:** a small auxiliary model —
  `gpt-4.1-mini` on the provider used for this run (the same roles run on
  `claude-haiku-4.5` on other providers).
- **Evaluator / evolution loop:** none at runtime; iteration is offline against a
  faithful local replay of scraped trials, gated by grader `score_detail`.
- **Runtime settings that mattered:** a bit-identical startup context so prompt
  caching pays off (~76% of input served from cache → ≈ $15 per 100-task run);
  reasoning effort `medium`; multi-provider routing with transient-retry backoff.
- **Were all models open-weight/local?** No — hosted frontier models. No
  open-weights badge claimed.

## E-commerce OS Reasoning

- **Catalogue and product matching:** hybrid. The model extracts a typed
  `task_spec` (brand, family, model, attributes, constraints); code does exact
  matching with a relaxation ladder and strict, direction-aware constraint
  comparison, then adds every qualifying SKU to the references. A pure-code
  verifier drops any cited SKU whose properties contradict the spec.
- **Inventory, warehouses, shipping, store coverage:** store counts are
  re-derived independently of the model, from SQL or — on PROD, where inventory
  is embedded in `/proc/locations/<city>/<store>.json` — from the filesystem
  (`available = max(on_hand − reserved, 0)`). Dispatch waves are routed by a
  pure-Python greedy solver maximizing expected net profit.
- **Customer records, baskets, orders, payments:** named records (a basket, a
  payment) are auto-added to the evidence only after an ownership check;
  cross-customer access is refused.
- **Merchant policies and addenda:** policy-bearing documents and every matching
  catalogue-count addendum are completed into the references deterministically.
- **Tickets, returns, refunds, escalations:** a refund's reversed payment is
  chained into the references from the return record's linked `payment_id`.
- **Audit trails, logs, evidence:** every record the model touches accumulates
  into a `seen_refs` ledger, which the enforcer chain canonicalizes against the
  world (case/variant normalization, existence-check via `stat`).

## Acting, Refusing, and Escalating

- **When may it mutate state?** Only after evidence-gated checks pass; a
  destructive verb is never resolved to "OK" without justification, and writes
  are never parallelized.
- **Authorization / identity / policy:** the actor is resolved from `/bin/id`;
  ownership and role are checked before private records are read or attached.
- **Pressure to discount / refund / checkout / disclose data:** security is
  assessed by *consequence* ("blast radius"), not a word blocklist. The same
  malicious text gets different treatment depending on what the action would
  actually do.
- **Refuse vs clarify vs escalate:** on a security refusal the agent **shields
  the very records it refuses to expose** — person records are stripped from the
  refusal so the refusal itself doesn't leak the thing it denied.

## Problems

- **The world re-instantiates every run.** The same task family returns with new
  wording and data, so any fix pinned to a literal task ID or string evaporated
  on the next run.
- **The model half-satisfied the exact-format contract** — "we have one" instead
  of `<COUNT:1>`, stray `OUTCOME_*` prefixes, prose where a `<NO>` was demanded.
- **The model found evidence but forgot to cite it** (or cited contradicting /
  hallucinated records), and references are graded as an exact set — a missing
  *and* an extra ref both cost points.
- **Aux-provider blackouts** (e.g. a dead model channel returning 400s) could
  wipe the verification/judge layer mid-run.

## Solutions

- **Prompt → harness.** Where the model kept tripping over the same class of
  task, the fix became a tool or a deterministic check, not another prompt line.
- **A multi-channel answer for multi-channel grading.** Outcome, references, and
  format are solved by separate layers. The terminal chain runs ~20 ordered
  steps with three invariants: *completers ADD references by union; verifiers
  DROP contradictions; re-derivers rewrite only the value token* — and an
  enforcer never silently changes what the agent decided. The reality-check
  stat-guard runs **last**, so it removes only genuinely missing paths without
  undoing legitimate additions.
- **Abstention by default.** Every re-deriver bounces (forces a re-answer) on
  disagreement and abstains on any ambiguity rather than guessing.
- **Provider resilience.** Three interchangeable inference providers with
  quirk-handling and provider-aware aux-model fallback.
- **Evaluation / debugging.** A faithful local harness rebuilt from scraped live
  trials reproduces most failures for cents; grader `score_detail` is the
  ground truth for *why* a task lost points; a task×run heatmap separates
  reliable wins from luck.
- **Kept deliberately simple.** Many speculative deterministic levers ship
  *off* behind flags — they proved brittle to re-instantiation, so the default
  config favors no-regression over chasing the last point.

## What Would You Improve Next?

The dominant residual is **variance, not capability** — the same agent swings
run-to-run because the world re-instantiates, and best-of-N plateaus from
independent noise. The clear next lever is **per-task self-consistency /
voting** (sample the main loop K times, reconcile the deterministic re-derivers
and reference sets across samples). It's designed but not yet built. After that:
a tighter fraud operating point and decomposing the ~2,900-line main-loop module.

## Lessons From ECOM1

- **Multi-channel grading demands a multi-channel answer.** Treating outcome,
  references, and format as one string leaves points on the table; the
  low-variance reference channel is where most recoverable points live.
- **Move recurring errors from the prompt into deterministic code** — it raises
  reliability *and* cuts cost, and it's re-derivable for free.
- **Add by union, abstain on ambiguity, never silently rewrite a decision;
  reconcile with reality last.**
- **Observability is the precondition for improvement.** On a re-instantiating
  benchmark, grader truth + traces + a faithful local replay are what let you
  tell signal from luck.
- **Architecture before autonomy.** Hand-build the skeleton; automate the
  refinement only once it's stable. And know the ceiling: a literal 100/100 is
  mathematically impossible here (the dispatch family maxes out ≈ 99.4).
