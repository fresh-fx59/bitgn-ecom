# BitGN ECOM1 Agent: a deterministic harness around a model dispatcher

> **Lineage.** This agent began as a fork of the official BitGN **PAC1**
> reference implementation — `github.com/bitgn/sample-agents/pac1-py` — and
> was ported to the ECOM1 benchmark. The PAC1 build reached 104/104 on its own
> PROD surface; the ReAct loop, the validator/enforcer pipeline, parallel
> reads, and the trace writer all survive from that lineage. What changed for
> ECOM1 is the runtime adapter, the domain helpers, and a much larger
> grounding-reference layer. This document describes the agent **as it ships
> today** (`v0.1.171`), not the PAC1 ancestor.

This is an architecture overview of the agent we built for the **BitGN ECOM1**
contest. The goal of the document is simple: a reader who has never seen the
code should come away understanding *how the agent actually works* — what the
model does, what deterministic code does, where the score comes from, and where
it hits a wall.

The one-line summary: **the model is a dispatcher inside a deterministic
harness.** The model reasons and chooses tools; everything that can be computed
exactly — security consequences, fraud clusters, store counts, the exact set of
grounding references — is computed by code, before or after the model speaks.

---

## 1. The challenge

BitGN ECOM1 is a simulated retail back-office operating system. The agent is
dropped into a Unix-like workspace (`/proc/...` record trees, `/bin/sql`,
`/bin/jq`, `/bin/id`, `/archive` exports, `/docs` policy notes) and asked to
complete a single operational task per trial: count stock across stores, resolve
a SKU from a fuzzy description, detect a fraud ring, approve or refuse a refund,
quote a pasted parts list, plan a dispatch wave, handle a prompt-injection
attempt, and so on.

There are two surfaces:

| Surface | Slug | Tasks | Notes |
|---|---|---|---|
| **DEV** | `bitgn/ecom1-dev` | 53 (`t01`–`t53`) | iteration target; reads are free |
| **PROD** | `bitgn/ecom1-prod` | 100 (`t001`–`t100`) | the scored leaderboard; rate-limited |

The single most important property of the benchmark: **the environment
re-instantiates on every run.** The same task *family* comes back with different
wording, different entities, and different underlying data each time. A fix
pinned to a literal task ID or a literal string evaporates on the next run. This
forces a design discipline that runs through the whole agent: **fix families,
not instances**, and prefer deterministic re-derivation over pattern-matching on
surface text.

---

## 2. What the grader rewards

A task is graded on up to three independent channels. You can lose points on any
one of them while getting the others right — which is why the agent treats the
answer as three separate artifacts, not one string.

1. **Outcome** — the decision/value itself. A count, a yes/no, a chosen SKU, a
   refusal, a clarification, a JSON plan. Graded against a fixed expected value.
2. **Grounding references** — the *exact set* of record paths that justify the
   answer (`/proc/catalog/<brand>/<sku>.json`, `/proc/payment-ledger/...`,
   `/uploads/<ocr>.md`, `/archive#<row>`, …). Graded as a set: a missing ref
   *and* an extra ref both cost points.
3. **Exact answer format** — a hard contract. `<COUNT:3>` not "we have three";
   `<YES>`/`<NO>` not a paragraph; a tab-separated table when one is demanded;
   no leaked `OUTCOME_*` markers.

The grounding-reference channel is where most of the recoverable points live: it
is **deterministic and low-variance** (the right set of records is a fact about
the world, not a judgement call), whereas the outcome on fraud/dispatch tasks is
inherently noisy. This is why so much of the harness is a *reference* machine.

---

## 3. Architecture at a glance

The agent runs a fixed pipeline per task. Cheap, deterministic, and aux-model
work brackets one expensive main-model loop in the middle.

```
                       ┌─────────────────────────────────────────────┐
  task text  ───┬────► │ (1) CLASSIFIER  (aux model, structured out)  │
                │      │     intent flags + entities, runs in parallel│
                │      └─────────────────────────────────────────────┘
                │      ┌─────────────────────────────────────────────┐
                ├────► │ (1) ROUTER  tier-1a regex → tier-1b normalize │
                │      │     +regex → tier-2 aux classifier → skill    │
                │      └─────────────────────────────────────────────┘
                │      ┌─────────────────────────────────────────────┐
                └────► │ (2) PREFLIGHT / PREPASS  (deterministic)      │
                       │     fraud 4-probe SQL union · actor /bin/id · │
                       │     synthetic bootstrap context               │
                       └───────────────────┬─────────────────────────┘
                                           ▼
                       ┌─────────────────────────────────────────────┐
                       │ (3) MAIN LOOP — model as dispatcher          │
                       │     native tool-calls (tool_choice=required, │
                       │     parallel reads) · 40-step budget ·        │
                       │     reactive skills · transient retry         │
                       └───────────────────┬─────────────────────────┘
                                           ▼  ReportTaskCompletion
                       ┌─────────────────────────────────────────────┐
                       │ (4) TERMINAL ENFORCER CHAIN (deterministic + │
                       │     aux judges) — ~20 ordered steps:          │
                       │     completers ADD refs (union) · verifiers   │
                       │     DROP contradictions · re-derivers rewrite │
                       │     the value token · stat-guard runs LAST    │
                       └───────────────────┬─────────────────────────┘
                                           ▼
                       ┌─────────────────────────────────────────────┐
                       │ (5) VALIDATOR + FORMATTER + VERIFY            │
                       │     terminal rule gates · count re-derivation │
                       │     · format/judge · one-shot verify          │
                       └───────────────────┬─────────────────────────┘
                                           ▼
                                    submit + trace
```

### Who does what, and on which model

The expensive reasoning is concentrated in exactly one place — the main loop.
Everything else is either a cheap aux-model call or pure code.

| Stage | Component | Handler | Responsibility |
|---|---|---|---|
| 1 | Classifier | aux model | intent flags, entities (basket id, actor, impersonation, checkout) |
| 1 | Router (tier 1) | code | regex on original + English-normalized task text |
| 1 | Router (tier 2) | aux model | skill classification on miss; confidence ≥ 0.6 |
| 1 | Task hints | code | advisory structured hint lines |
| 2 | Fraud preflight | code + SQL | 4 deterministic probes, union, surface as observation |
| 2 | Actor identity | code | parse `/bin/id` → actor employee id |
| 2 | Bootstrap context | code | synthetic “tool call” pairs that seed the loop |
| 3 | **Main loop** | **main model** | **reasoning, tool choice, the answer** |
| 3 | Reactive router | code + aux | mid-run skill injection on sensitive paths |
| 4 | Completers (×9) | code (+ SQL) | ADD missing grounding refs by union |
| 4 | Verifiers / strippers | code | DROP contradicting / private / dead refs |
| 4 | Re-derivers | code (+ SQL/FS) | recompute the value token, abstain on doubt |
| 4 | Ref judges | aux model | decide ambiguous catalog/cart ref sets |
| 4 | Stat-guard | code | drop refs the runtime confirms missing — **last** |
| 5 | Terminal validator | code | grounding/outcome rule gates |
| 5 | Format/judge | aux model | enforce the exact-format contract |
| 5 | Verify | main model | one-shot re-check, hard-capped at 1/task |

**Main dispatcher model:** a frontier OpenAI reasoning model
(`gpt-5.3-codex` / `gpt-5.4` / `gpt-5.5` depending on provider and run),
reasoning effort `medium` (empirically lower-variance than `high`).
**Auxiliary model:** Claude Haiku 4.5 (or `gpt-4.1-mini` on the linkapi
provider) for the classifier, router tier-2, ref judges, and the formatter.
**Deterministic code:** security, all domain computation, evidence, and
references.

---

## 4. The main loop: the model as a dispatcher

The loop is a small state machine (`agent.py:AgentLoop.run`). At each step the
model is forced to emit a tool call; the harness runs it, feeds back the result,
and repeats until the model calls `ReportTaskCompletion`. The step budget is
**40**; tool-result payloads are capped to keep context lean.

There are two execution paths behind one interface, because providers differ:

- **Native tool-calling** (`AGENT_TOOLCALLING=1`, `openai_toolcalling.py`):
  per-tool flat JSON schemas, `tool_choice="required"`, results returned with
  `role="tool"`. This is the preferred path.
- **Salvage / chat** (`openai_compat.py`): a JSON-envelope contract parsed from
  the message body, used where a provider mangles native tool calls. It first
  tries structured parsing (`beta.chat.completions.parse`) and falls back to
  **streaming + manual JSON extraction** — necessary because one proxy returns
  `content: null` on the non-streaming path.

**Parallel reads.** When the model's primary action is read-only (read / list /
search / find / tree / exec), the harness lets it batch several reads and
dispatches them concurrently on a thread pool, feeding the results back as one
labelled message. Writes and mutations are never parallelized.

**Synthetic start.** Rather than dumping the workspace into one giant system
message, the initial context is seeded as synthetic *tool-call/result pairs*
(actor identity, fraud-probe observations, schema notes). The model reads its
own “prior actions” and continues naturally — this measurably improves
comprehension over a wall of preface text and keeps the first two messages
bit-identical across tasks, which is what makes the prompt cache pay off (more
on cost in §10).

**Reliability.** The benchmark runs against shared, sometimes-flaky inference
proxies, so the loop is wrapped in defensive machinery:

- a bounded transient-retry backoff (`0.5s → 60s`, ~106s total) that classifies
  connection resets, `unexpected eof`, upstream 5xx, and concurrency-limit
  errors as retryable;
- a 30-second per-call HTTP timeout with an extended post-model-unload wait;
- an in-flight concurrency semaphore so batched trials don't stampede the
  provider;
- **multi-provider support** — `cliproxyapi`, `CloseRouter`, and `linkapi` are
  all wired, each with its own quirks handled (reasoning-effort sent in both
  flat and nested shapes; the `content: null` streaming workaround; provider-
  aware aux-model selection so a dead Haiku channel falls back to
  `gpt-4.1-mini`).

---

## 5. The classifier and the preflight cascade

Before the main loop spends a single expensive token, cheap deterministic and
aux work front-runs the obvious decisions.

**The intent classifier** is a general aux-model call returning structured flags
and entities (checkout intent, basket id, manager approval, impersonation
attempt, employee-contact request, …). It runs in parallel with routing and
**degrades gracefully** — if the aux provider is down, the agent simply proceeds
on the main loop alone. The same aux endpoint powers router tier-2 and the
mid-run reactive router.

**The fraud preflight** is the most consequential deterministic gate. On a fraud
task it runs four SQL probes — shared card fingerprint, shared device
fingerprint, time-impossible cross-store pairs, coordinate cluster — reads each
matched payment record, and **surfaces the union to the model as an
observation**. This removes the single noisiest decision the model would
otherwise improvise (“which pattern do I probe, and in what order?”) and turns
the model's job into *verify-and-cite* rather than *investigate-and-decide*.

The rest of the cascade is lighter: actor-identity extraction from `/bin/id`
(needed for ownership and privacy checks), and a schema-bootstrap stub kept for
adapter shape-compatibility. The design rule throughout: **preflight surfaces
hints and evidence; it does not pre-commit the answer.** Every probe fails
silently rather than blocking the task.

---

## 6. Domain helpers

The recurring insight of the whole project: *where the model trips over the same
class of task again and again, you help it earlier — with a tool or a
deterministic check — not with another line in the prompt.* Each helper owns one
failure family. The division of labour is always the same: **the model is
responsible for meaning and tool choice; code is responsible for everything that
can be done deterministically.**

**Catalog / SKU.** Resolving a fuzzy product description to the right SKU(s) is
hybrid: the model extracts structure (brand, family, model, attributes,
constraints) into a typed `task_spec`; code does exact matching with a
relaxation ladder and strict, direction-aware constraint comparison, then
**adds** every qualifying SKU to the references. A second pure-code verifier
*drops* any cited SKU whose properties contradict the spec. The pair —
`sku_completer` (add) + `sku_verifier` (drop) — is asymmetric on purpose:
completion is generous, verification is conservative, and verification owns the
final cut.

**Fraud.** SQL alone failed here (truncation, drifting heuristics), so fraud is a
dedicated helper. It builds a connected component over `device ∪ payment_method`
edges among rapid, cross-store, same-customer payments, with a precision gate
that excludes single-device legitimate bursts. Again an asymmetric pair:
`fraud_recall_completer` *adds* the canonical fraud set, `fraud_cluster_filter`
*drops* single-device false positives. Honest verdict: fraud is a genuine wall
(see §12).

**Count.** Store counts are re-derived independently of the model. Three
variants exist — SQL-based (`count_rederive`), filesystem-based for PROD where
inventory is embedded in `/proc/locations/<city>/<store>.json`
(`fs_count_rederive`), and a reference-free override for templated count tasks
(`refless_count_override`). The contract is strict: a re-deriver **bounces**
(forces the model to re-answer) on disagreement rather than silently rewriting,
and **abstains** on any ambiguity — an unresolvable store, mismatched variants,
an unparseable threshold.

**Dispatch.** A pure-Python greedy solver routes packages over lanes to maximize
expected net profit (margin × P(on-time) − transport cost), reliability- and
congestion-aware, and emits a validated JSON plan. No model, no SQL.

**Chaining completers.** Several small deterministic helpers chain one cited
record to its dependency: a return → its linked payment
(`refund_payment_completer`), an employee → their home store
(`store_back_completer`), a catalogue-count task → all matching addendum docs
(`addenda_completer`).

A deliberate design choice worth calling out: **most of these helpers ship
env-gated OFF.** Only `sku_completer`/`sku_verifier`, the fraud preflight +
recall + filter, and `addenda_completer` are always-on. The rest
(`count_rederive`, `fs_count_rederive`, `refless_count_override`,
`count_ref_completer`, `quote_ref_completer`, `dispatch_planner`,
`refund_payment_completer`, `store_back_completer`, the three `exo_ref_rules`,
…) sit behind `BITGN_USE_*` flags, default-off, because they proved **brittle to
re-instantiation** — a shape regex that wins on one wording loses on the next —
or net-neutral. This is both a strength (no silent regressions; every lever is
A/B-gated) and a weakness (a lot of the harness is dormant machinery). See §13.

---

## 7. The terminal enforcer chain

This is the heart of the agent. When the model reports completion, the answer
passes through a single **ordered chain of ~20 enforcement steps**
(`agent.py:_post_process_terminal`) before submission. Each step is one of three
kinds:

- **Completers — ADD references by union.** They restore evidence the model
  found but forgot to cite, or the records a correct answer logically rests on.
  They never touch the value or the message body.
- **Verifiers / strippers — DROP references.** Contradicting SKUs, private
  person records on a privacy refusal, dead paths.
- **Re-derivers — rewrite the value token only.** Count overrides and the
  dispatch plan replace the answer value (never the references) when code is
  confident.

Two invariants make the chain safe:

1. **Enforcers add references by union and never rewrite the model's numeric
   answer** — except the explicit re-derivers, which touch the value token and
   nothing else. An enforcer can make the *grounding* more complete or more
   honest, but it cannot quietly change *what the agent decided*.
2. **The stat-guard runs last.** The final step stat-checks every `/proc`
   reference against the live runtime and drops the ones confirmed missing.
   Running it last means every earlier union-based completer has already added
   its refs, and the guard removes only genuinely hallucinated paths without
   undoing legitimate additions.

There is also an optional **LLM-judge short-circuit** at the top: if enabled and
confident (≥ 0.5), an aux judge can decide the whole reference set in one call
and skip the legacy chain; on low confidence or failure it falls through to the
deterministic steps. It ships default-off — the deterministic chain is the
trusted path.

Representative order (operation type in brackets):

| # | Step | Op | Default |
|---|---|---|---|
| 0 | LLM judge short-circuit | decide-all | off |
| 1 | refusal-cite enforcer (security refusals) | drop + re-add | on |
| 2 | SKU verifier (catalog refs) | drop | on |
| 3 | policy-triple cite completer | add | on |
| 4–14 | store/quote/count/ref-judge/cart/refless/dispatch/fraud/refund | add or rewrite | mostly off |
| 15 | addenda completer | add | on |
| 16–18 | count_per_store SKU completer + verifier pass | add + drop | on (by kind) |
| 19 | yes_no_sku family completer | add | on (by kind) |
| 20a | crosslist filter / discount-emp ref | drop / add | off |
| 20b | **stat-guard** | drop missing | off, **last** |

---

## 8. Grounding references as a first-class output

Because references are graded as an exact set, the agent treats them as a
separate pipeline rather than a by-product of the message. Every record the
model touches is accumulated into a `seen_refs` ledger during the loop; the
enforcer chain then canonicalizes that evidence against the world:

- **canonicalize** path case and variants, confirm existence via `stat`;
- **auto-add** records the user named (basket, payment) after an ownership
  check, and SKUs surfaced in the final message;
- **split by meaning** — move misplaced records into the documents bucket;
- **cut explored-but-not-final** — keep only the evidence the answer rests on;
- **shield private data** — strip person records from a security refusal so the
  refusal itself doesn't leak the thing it refused to disclose;
- **link** dependent records (a refund's reversed payment).

The principle: **the model decides; code enforces consistency and safety.** Code
never guesses *intent*; it only makes the evidence honest, complete, and legal.

---

## 9. The final-answer formatter and validators

The exact-format contract is easy for a reasoning model to half-satisfy
("we have one" instead of `<COUNT:1>`, a stray `OUTCOME_*` prefix, prose where a
`<NO>` was demanded). Two components defend it:

- **`format_validator`** — pure PyYAML, no model. It validates frontmatter/YAML
  on every write so a malformed structured write is caught on the same step
  instead of failing silently at grading time.
- **`judge_enforcer`** — an aux-model formatter in structured-output mode. It
  receives the task, the current answer, the outcome and references, and the
  format rules, and returns *only* a corrected visible message. It is
  **reformat-only**: it never re-solves, never changes the outcome or the
  numeric value, filters its own add/drop suggestions against `seen_refs`, and
  abstains below confidence 0.5. On any failure it returns the original answer
  untouched.

Above these sits the **terminal validator** (`validator.py`): deterministic rule
gates (grounding references must have been read; outbox attachments must exist;
no destructive verb resolved to OK without evidence) plus a one-shot
re-verification step, hard-capped at one extra model call per task.

---

## 10. Observability and the iterate-fix loop

You cannot improve a system you cannot see, and on a re-instantiating benchmark
you especially cannot improve by eyeballing one run. The agent is built around
three observability layers:

- **Traces.** Every step is written to a per-task JSONL trace with a versioned
  schema; an architecture log tags each enforcer action by category
  (`REFS_DROP`, `VALIDATOR_T1/T2`, `TERMINAL`, …) with task/run context injected
  into every record. You can reconstruct exactly which deterministic layer
  touched an answer.
- **Grader truth.** `scripts/fetch_run_scores.py` and `fetch_trial_detail.py`
  pull per-task scores and the grader's own `score_detail` verdicts
  (read-only, no run cost) — the canonical ground truth for *why* a task lost
  points (which ref was missing, which was extra).
- **Heatmaps and intent reports.** A red→green task-by-run heatmap and an
  intent-classified pass-rate report turn dozens of runs into a single picture
  of what is reliable versus what merely got lucky.

Underneath sits a **faithful local harness** (`LocalEcomClient`) rebuilt from
scraped live trials, so most failures can be reproduced and fixed for cents
before a single PROD credit is spent. The operational loop is fixed: snapshot →
reproduce 5× → diagnose against grader truth → broad-family fix → 5× local A/B →
regression suite → bench gate.

### Cost

One full 100-task PROD run on `gpt-5.4` with a warm prompt cache costs
**≈ $14.64**: ~13.7M prompt tokens (75.8% served from cache), ~250K output
tokens, ~531 model calls (≈5.3/task). The cache is load-bearing — a cold run
costs ≈ $38, and `gpt-5.5` runs ≈ $29 warm / $76 cold. The aux layer is
negligible (≈ $0.30/run). The architectural levers that keep this low: a small
aux model, a short bit-identical startup context (cache hits), computation moved
into code instead of extra model turns, and batched runs.

---

## 11. Evolution: from PAC1 to DEV to PROD

The agent grew in three distinct phases, and the sequencing was deliberate:

1. **Interactive architecture (the PAC1 inheritance).** The core contours — the
   ReAct loop, the validator/enforcer split, parallel reads, the trace writer —
   were formed by hand on the PAC1 reference implementation, where the build
   reached 104/104. This stage needs human judgement: you cannot autonomously
   refine a skeleton that doesn't exist yet.
2. **DEV stabilization.** Ported to `ecom1-dev` (then 42, now 53 tasks), the
   settled skeleton became the substrate for an autonomous iterate-fix loop. On
   the stable surface the deterministic stack reached **42/42** repeatedly, and
   **48/53** after the task set grew.
3. **PROD adaptation.** `ecom1-prod` is a different world — 100 tasks, different
   state, sealed grading, full re-instantiation. The *architecture and the
   observability tooling survived the move unchanged*; what had to be rebuilt
   was the calibration of the deterministic layers against a moving target.

The takeaway: **autonomous self-improvement requires a pre-assembled
architecture.** Automating refinement while the core is still unsettled wastes
effort; once the skeleton and the observability are in place, the loop pays off.

---

## 12. Strengths

- **Multi-channel grading is met with a multi-channel answer.** Outcome,
  references, and format are handled by purpose-built layers instead of hoping
  one model emission satisfies all three. The reference machine in particular
  recovers the most points for the least variance.
- **Recurring errors live in code, not in the prompt.** Fraud clusters, store
  counts, dispatch plans, and reference completeness are computed deterministically
  and re-derivable for free, which both raises reliability and slashes cost.
- **Asymmetric add/drop pairs** (completer + verifier) give generous recall with
  conservative precision, and a single clear owner for the final cut.
- **Safety by construction.** Enforcers add by union and never silently rewrite a
  decision; re-derivers abstain on ambiguity; the stat-guard runs last; security
  refusals shield the records they refuse to expose.
- **Observability and a faithful local harness** make blind iteration possible:
  grader-truth `score_detail`, traces, heatmaps, and cent-cost local replay.
- **Provider and cost resilience** — three interchangeable inference providers
  with quirk handling, and a prompt-cache discipline that keeps a 100-task run
  under $15.

## 13. Weaknesses and the ceiling

This document is meant to be honest, so the limits are stated plainly.

- **There is a hard score ceiling, and we sit just below it.** Best PROD runs
  land around **80–82/100, mean ≈ 0.89**. A literal 100/100 is *mathematically
  impossible*: the dispatch family is scored against a transport-free
  gross-margin baseline whose maximum is ≈ 99.4, not 100.
- **Variance, not capability, is the dominant residual.** Because the world
  re-instantiates, the same agent swings run-to-run, and best-of-N plateaus
  around 92–93 from independent noise. The real remaining lever —
  **per-task self-consistency / voting — is designed but not implemented.**
- **Fraud and dispatch are genuine walls.** Fraud scores are wildly
  instance-variant (0.20 → 1.0, mean ≈ 0.78) for any rule we or others have
  tried; dispatch has no efficiency loophole to exploit. Neither yields to more
  prompt or more code.
- **Much of the harness is dormant.** A large fraction of the domain helpers
  ship env-gated OFF because they are brittle to re-instantiation (surface-shape
  regexes that don't survive re-wording) or net-neutral. The conservatism is
  principled — every lever is A/B-gated and nothing regresses silently — but it
  means the agent carries machinery it cannot safely switch on.
- **Aux-LLM blackouts.** Provider outages (e.g. a dead Haiku channel returning
  400s) can wipe the verification/judge layer mid-run. The mitigation —
  provider-aware aux selection and pushing logic into deterministic code — is in
  place, but a fully model-dependent step remains a single point of failure.
- **Grader inconsistencies.** A few reference rules (e.g. the `(but not X)`
  clause, a basket-refusal contradiction) are not reconcilable by any static
  rule, so a handful of flippers can't be deterministically pinned.
- **One large state machine.** The main loop and its ~20-step enforcer chain
  live in a ~2,900-line module. It is heavily traced and tested, but the
  hardcoded chain order is a real maintainability cost.

---

## 14. Key principles to take away

1. **The model is a dispatcher, not a process keeper.** It reasons and chooses
   tools; the harness owns control flow, budgets, and the contract.
2. **Recurring errors move from the prompt into deterministic code.** A wall the
   model keeps hitting becomes a tool or a check, not another sentence.
3. **Multi-channel grading demands a multi-channel answer.** Solve outcome,
   references, and format as separate problems.
4. **Add by union, abstain on ambiguity, never silently rewrite a decision.**
   The harness makes the answer more complete and more honest, not different.
5. **Stat-check reality last.** Reconcile against the live world only after every
   additive step has run.
6. **Observability is the precondition for improvement.** On a re-instantiating
   benchmark, grader truth + traces + a faithful local replay are what let you
   tell signal from luck.
7. **Architecture before autonomy.** Hand-build the skeleton; automate the
   refinement only once it is stable.

---

## 15. Models, in brief

| Role | Model | Notes |
|---|---|---|
| Main dispatcher | `gpt-5.3-codex` / `gpt-5.4` / `gpt-5.5` | reasoning effort `medium`; the only expensive reasoning |
| Auxiliary | Claude Haiku 4.5 (or `gpt-4.1-mini`) | classifier, router tier-2, ref judges, formatter |
| Deterministic code | — | security, domain computation, evidence, references |

## 16. Sources and materials

- BitGN PAC1 reference implementation (origin of this build):
  `github.com/bitgn/sample-agents/pac1-py`
- This repository: the ECOM1 agent (`src/bitgn_contest_agent/`), `AGENTS.md`
  (operational rules), `docs/PROMPT_RULES.md`, `docs/ECOM_RUN_COST.md`.
