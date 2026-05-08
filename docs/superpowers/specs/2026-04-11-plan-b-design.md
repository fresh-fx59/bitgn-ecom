# BitGN PAC1 Agent — Plan B Design Spec

**Date:** 2026-04-11
**Status:** Approved for implementation planning
**Repo:** `bitgn-contest-with-claude`
**Predecessor:** Plan A (`2026-04-10-bitgn-agent-design.md`) — shipped v0.0.33 with 22/43 ratchet floor on `bitgn/pac1-dev`.
**Goal:** Prove the iteration loop. Stand up the measurement apparatus that lets future agent changes be evaluated defensibly, and land one bounded behavior change through it. Ship v0.1.0.

---

## 0. Context and scope

### 0.1 Where Plan A left us

Plan A delivered a working BitGN PAC1 agent and a committable ratchet floor:

| Fact | Value |
|---|---|
| Ratchet floor (`bitgn/pac1-dev`) | **22/43 passes (51.2%)** |
| Commit | `1623b40` (v0.0.33) |
| Artifact | `artifacts/bench/1623b40_20260410T181832Z.json` |
| Model | `gpt-5.3-codex` at `reasoning_effort=medium` via cliproxyapi |
| Run config | `--runs 1 --max-parallel 4` |
| Variance observation | Back-to-back runs at the same commit scored 22/43 and 26/43 — spread of ~9 pp |

The 9 pp back-to-back spread is the load-bearing observation for Plan B: **we cannot tell real regressions from noise with a single-run gate.** Every subsequent iteration requires a variance-aware gate before agent-behavior changes can be evaluated defensibly.

### 0.2 Failure cluster analysis from the baseline artifact

Of 21 failing tasks at the baseline, clustered heuristically:

| Cluster | Count | Example tasks | Dominant pattern |
|---|---|---|---|
| Inbox / identity | 10 | t21, t22 | Agent notices conflict/ambiguity in scratchpad but does not emit `OUTCOME_NONE_CLARIFICATION`; writes both conflicting values or trusts display names over email addresses |
| Wrong action (grader disagrees) | 7 | t07, t20, t36 | Agent reports `OUTCOME_OK`, grader scores 0 |
| False refusal | 2 | — | `DENIED_SECURITY` / `NONE_UNSUPPORTED` on tasks the grader expected to complete |
| Timeout | 1 | t30 | Hit step cap |
| Calendar / other | 1 | — | — |

The inbox cluster is ~48% of failures and is concentrated on patterns that the repo-root `AGENTS.md` *already* has rules for — authority hierarchy, conflict resolution, inbox identity verification — but those rules never got ported into the runtime system prompt in Plan A. That is the defensible behavior change Plan B ships.

### 0.3 PROD context

The BitGN PAC1 contest PROD benchmark opened on 2026-04-10 at 13:00 GMT+2. Plan B is intentionally scoped **`pac1-dev` only**. PROD measurement, PROD scoring, and the SDK upgrade that would support richer tool primitives on PROD are all deferred to **Plan B-prime**, which is opened as a follow-up issue when Plan B closes.

### 0.4 What Plan B is NOT

- Not an SDK upgrade. The new BitGN proto surface (bounded reads, ranged writes, depth-limited tree, RFC 3339 context time) is real and verified via spike, but Plan B does not touch `bitgn-local-sdk`, `adapter/pcm.py`'s request plumbing, or the `NextStep` schema. All of that is B-prime.
- Not a tool-usage prompt rewrite. Teaching the agent to prefer `read(start_line, end_line)` over whole-file reads is a behavior change whose value only lands once the SDK is upgraded. B-prime.
- Not a multi-benchmark harness. `bitgn/pac1-dev` only.
- Not a provider abstraction rewrite. The existing `Backend` Protocol is fine; wiring the dead `max_inflight_llm` knob is in scope, replacing the Protocol is not.
- Not a rewrite of `prompts.py`'s structure beyond the narrow "runtime rules module" split required for Phase 3. If `prompts.py` wants a broader refactor later, that is its own plan.

---

## 1. Architectural overview

### 1.1 The spine

Plan B is a measurement apparatus, not a behavior change. Everything interesting the plan does is either (a) building the apparatus or (b) running one carefully-scoped behavior change through it. Three load-bearing components carry the plan end to end:

1. **The gate** — `bench_summary.json` produced by `bitgn-agent run-benchmark`, extended with multi-run variance aggregation (min, median, 95% percentile bootstrap CI). `overall.pass_rate` remains the primary number; it is now accompanied by a confidence band so real changes can be told from noise.

2. **The triage tool** — a new CLI, `bitgn-agent triage`, that takes a bench artifact and groups failing tasks into clusters (`inbox`, `wrong_action`, `false_refusal`, `timeout`, `calendar`, `other`) using heuristics over `task_text`, `terminated_by`, reported `outcome`, and grader score. Supports a diff mode (`--before A.json --after B.json`) that shows which clusters moved. This is how Phase 3's improvement is made visible in one command.

3. **The ratchet** — `artifacts/bench/README.md` plus the `project_bench_ratchet` memory file, updated only when a gate run shows a new floor with statistical confidence. The floor is the contract with future PRs.

### 1.2 Phase sequence

```
 Phase 1 plumbing ──► Phase 2 burst + baseline ──► Phase 3 rules rewrite ──► Phase 4 close
        │                       │                         │                       │
        ▼                       ▼                         ▼                       ▼
   no runtime          operating point           before/after triage       v0.1.0 tag +
   behavior            + v0.1.0-rc               + ratchet decision         close-out report
   change              + new baseline            (on feature branch)        + B-prime issue
```

**Why this ordering:**

- **Phase 1 first** because we cannot measure anything defensibly without variance and triage. Plumbing is cheap; landing it before any behavior change means Phase 2 and Phase 3 are measured through the new apparatus from the start.
- **Phase 2 second** because without the parallelism speedup, measuring Phase 3 at `--runs 3` takes long enough to make the iteration loop unusable. Running the burst discovery as its own discrete step (rather than piggybacking on the first real gate run) prevents a throttled cliproxyapi from poisoning the very baseline we're trying to capture.
- **Phase 3 third** because it is the only phase that intentionally changes agent behavior. We want maximum measurement capacity behind us when we make it.
- **Phase 4 fourth** because close-out requires all prior artifacts to be final.

### 1.3 The plumbing-only invariant

Phase 1's defensibility rests on a strict rule: **no Phase 1 PR touches the LLM call path, the prompt text, the `NextStep` Pydantic schema, or `AGENTS.md`.** This invariant is what lets us skip a "re-baseline at HEAD" bench run after Phase 1 lands — smoke tests plus the invariant together substitute for a full-bench re-run.

The invariant is enforced by **three independent checks**:

1. **Commit-message marker.** Every Phase 1 commit must include the literal line `plumbing-only: no behavior change`. This is machine-checkable.
2. **Code-review gate on protected paths.** The reviewer verifies no diff touches: `src/bitgn_contest_agent/adapter/`, the LLM client module, `src/bitgn_contest_agent/prompts.py`, `NextStep` schema definitions, or `AGENTS.md`.
3. **Smoke tests on every PR.** The 5-task subset `t02, t42, t41, t15, t43` runs against live cliproxyapi with hardcoded `--max-parallel 5 --max-inflight-llm 8`. Pass criterion: `t02/t42/t41` pass, `t15/t43` fail, wall-clock under 180s.

If any Phase 1 PR violates the invariant — whether by accident or because a scope expansion became necessary — the PR must include a full `--runs 3` bench re-run against `pac1-dev` to re-establish baseline before Phase 2 can proceed.

### 1.4 Deliverables at Plan B close

- `bitgn-contest-agent` v0.1.0 with tuned parallelism defaults
- `bench_summary.json` schema v1.1 (variance + token + harness_url fields, additive-only)
- New `bitgn-agent triage` CLI subcommand
- `scripts/burst_test.py` standalone diagnostic harness
- `src/bitgn_contest_agent/prompts/runtime_rules.py` populated and wired into the system prompt
- `AGENTS.md` pruned to developer-workflow content only
- Phase 2 baseline + Phase 3 result artifacts under `artifacts/bench/`
- Close-out report at `docs/superpowers/reports/YYYY-MM-DD-plan-b-closeout.md`
- Updated ratchet floor (conditional on Phase 3 improvement)
- Plan B-prime follow-up issue

---

## 2. Phase 1 — Gate-enabling infra

Seven discrete components, each with a single responsibility. All are plumbing only; none change runtime behavior.

### 2.1 `bench_summary.json` schema v1.1

Additive bump; backward-compatible with v1.0.

**New top-level fields under `overall`:**

- `runs_per_task` (int, defaults to 1 for legacy artifacts)
- `pass_rate_min`, `pass_rate_median` — per-run floor and middle across N runs
- `pass_rate_ci_lower`, `pass_rate_ci_upper` — 95% percentile bootstrap CI
- `total_input_tokens`, `total_output_tokens`, `total_reasoning_tokens`
- `trace_dir` — relative path to the `logs/<ts>/` directory containing per-task traces
- `divergence_count` — total across all tasks (see §2.5)

**New per-task fields:**

- `passes_per_run` — list of 0/1 per run
- `input_tokens`, `output_tokens`, `reasoning_tokens`
- `harness_url` — captured from the trial's StartPlayground/StartTrial response
- `divergence_steps` — per-task count of divergence events

`schema_version` bumps to `"1.1.0"`. A round-trip test loads `artifacts/bench/1623b40_20260410T181832Z.json` (v1.0) through the new parser and verifies the new fields default safely. This test is part of the Phase 1 CI gate.

### 2.2 Multi-run aggregator

New module: `src/bitgn_contest_agent/bench/aggregate.py`. Exports one pure function:

```python
def aggregate_runs(per_run: list[TaskRunResult]) -> OverallSummary: ...
```

Standard percentile bootstrap, 1000 resamples, fixed RNG seed for deterministic tests. No SciPy dependency — implemented against the Python standard library.

Wired into the existing `run-benchmark` CLI path where `--runs N` is already parsed but currently fans out only to the first run. Phase 1 makes the fan-out real: `--runs 3` produces three full task passes and feeds the results into the aggregator.

Unit tests use hand-computed expected CIs on synthetic inputs (e.g. 3 runs of 43 tasks, all passes → CI should be [1.0, 1.0]; 3 runs with one run at 21/43 and two at 22/43 → median 22, CI band explicit). Also tests the backward-compat case where `aggregate_runs` is called with N=1.

**Boundary:** the aggregator never touches the LLM call path. It consumes only completed `TaskRunResult` records.

### 2.3 Failure triage tool

New CLI subcommand: `bitgn-agent triage <bench_summary.json> [--before A.json --after B.json] [--json]`.

**Clustering rules (applied in order, first match wins):**

1. `timeout` — `terminated_by in {"timeout", "max_steps"}`
2. `false_refusal` — reported `outcome in {OUTCOME_DENIED_SECURITY, OUTCOME_NONE_UNSUPPORTED}` AND grader score == 0
3. `inbox` — `task_text` matches `/inbox|email|message|sender/i` AND grader score == 0
4. `calendar` — `task_text` matches `/calendar|meeting|event|schedule/i` AND grader score == 0
5. `wrong_action` — reported `outcome == OUTCOME_OK` AND grader score == 0
6. `other` — everything else failing

**Output shapes:**

- **Markdown (default):** a table with columns `cluster | count | example_tasks`, followed by a short per-cluster drill-down section listing each failing task's ID, reported outcome, and a 1-line task summary.
- **JSON (`--json`):** the same data in a machine-readable structure for downstream tooling.
- **Diff mode (`--before/--after`):** markdown table with columns `cluster | before | after | delta | direction`, where direction is `↑` / `↓` / `=`. The diff is the primary Phase 3 evaluation artifact.

New module: `src/bitgn_contest_agent/bench/triage.py`. Reads `bench_summary.json`, walks the `trace_dir` it points to for per-task details (`terminated_by`, final outcome, task text), applies the clustering rules. Pure function over filesystem inputs; no network calls.

**Acknowledged weakness:** keyword-based clustering is noisy. False positives are likely (e.g., a task mentioning "email" that isn't an inbox task). Good enough as a progress signal within Plan B, not good enough to auto-classify arbitrary future benchmarks. The clustering rules are documented in the module docstring so reviewers can cross-check by hand when a diff looks suspicious.

### 2.4 Token accounting + harness_url capture

Extends the existing `TraceWriter` to record per-step LLM token counts (input / output / reasoning where the provider surfaces them) and capture `harness_url` once at trial start from the `StartPlayground` or `StartTrial` response.

Rolled up by the aggregator into the schema v1.1 summary fields.

**Boundary:** modifies `trace_writer.py` only. The LLM client itself is unchanged — it already returns token counts in its response metadata; we just weren't persisting them. A first-PR-of-Phase-1 spike verifies this assumption on the live client. If the client swallows token metadata, Phase 1 scope grows by one PR to plumb token counts through the client's response handler, still within the plumbing-only invariant because LLM *call semantics* do not change — only what we persist from the response.

### 2.5 Divergence counter

New module: `src/bitgn_contest_agent/bench/divergence.py`. A post-hoc analyzer that reads trace files and counts steps where the agent's `current_state` scratchpad contains any conflict-signaling keyword but the next action is *not* `report_completion(OUTCOME_NONE_CLARIFICATION)`.

**Initial keyword list:** `conflict`, `contradict`, `contradictory`, `ambiguous`, `ambiguity`, `differ`, `differs`, `differing`, `both`, `either`, `versus`, `vs\.`, `instead of`, `inconsistent`, `unclear`, `unsure`.

**Rolled up** per-task (`divergence_steps`) and per-run (`divergence_count` in `overall`).

**Role in Plan B:** divergence is a **tertiary supporting signal**, not a gating signal. The primary signals for evaluating Phase 3 are pass_rate change (gate) and cluster movement (triage diff). Divergence exists to answer the specific question "did the scratchpad→CLARIFICATION link actually fire, or is the prompt not reaching the model?" A Phase 3 result where pass_rate is flat but divergence dropped meaningfully is the "flat-but-principled" case (§4.6). A Phase 3 result where everything else moves but divergence does not is a **wiring bug**, not a rule-content failure.

**Acknowledged weakness:** keyword matching over free-form scratchpad is heuristic. The keyword list is calibrated against the known t21/t22 traces (which should register as divergent) and negative fixtures from tasks where scratchpad mentions "conflict" or "both" benignly. False negatives after Phase 3 are possible if the agent's post-rewrite scratchpad wording shifts. This is documented in the module docstring so humans can eyeball traces when divergence numbers look suspicious.

### 2.6 Smoke test subset

Defined as a module-level constant:

```python
SMOKE_TASKS = ["t02", "t42", "t41", "t15", "t43"]
```

Selected for short median step counts (all ≤5 per the baseline artifact), fixed pass/fail expectations (`t02/t42/t41` pass, `t15/t43` fail), and diverse task types. Invoked via a new `--smoke` flag on `run-benchmark`:

```
bitgn-agent run-benchmark --benchmark bitgn/pac1-dev --smoke
```

Smoke runs use hardcoded `--max-parallel 5 --max-inflight-llm 8` independent of the (as-yet-untuned) parallelism defaults. Target wall-clock 60–90s; hard ceiling 180s fails the CI check as a "smoke is mysteriously slow" signal.

Smoke runs are **not** scored against the ratchet. They are sanity checks only.

### 2.7 Plumbing-only invariant definition-of-done

Every Phase 1 PR must satisfy all three enforcement checks (commit-message marker, protected-path review, smoke test) described in §1.3. Failure of any check blocks merge. If a PR genuinely needs to touch a protected path (e.g., to plumb token counts through the LLM client response handler per §2.4), the PR must include a full `--runs 3` bench re-run artifact and the commit message replaces the standard marker with `plumbing-only: justified exception — see bench artifact <path>`.

---

## 3. Phase 2 — Parallelism discovery + baseline + v0.1.0

Seven components. The first three are infrastructure, the middle two are the discovery, and the last two lock in the result.

### 3.1 Wire the `max_inflight_llm` semaphore

`config.py` defines `max_inflight_llm: int = 6` but nothing consumes it. Phase 2 wires it as a `threading.Semaphore` at the boundary where the SGR loop calls the `Backend` Protocol.

**Design decisions:**

- **One global semaphore**, not per-task. The bottleneck is cliproxyapi as a whole, not any one task loop.
- **Semaphore wraps only the `backend.call(...)` site.** The Protocol itself is unchanged. The LLM client is unchanged.
- **`max_parallel_tasks` and `max_inflight_llm` are independent knobs.** `max_parallel_tasks` is the number of concurrent task execution loops (ThreadPoolExecutor workers); `max_inflight_llm` is the max simultaneous LLM calls across *all* loops. With SGR, every step is basically an LLM call, so the two will be close in practice — but they are separable in config and plausibly diverge in B-prime.

**Unit test:** spin up N mock backends with artificial delays, set semaphore to K, verify peak concurrent calls never exceed K using a shared atomic counter.

### 3.2 Run metrics instrumentation

New file written alongside `bench_summary.json`: `run_metrics.json`. Contains:

- `wall_clock_start`, `wall_clock_end`, `wall_clock_seconds`
- `peak_inflight_llm` — gauge sampled every 250ms during the run
- `latency_p50`, `latency_p95`, `latency_p99`, `latency_max` — per-call single LLM call latency histogram
- `total_llm_calls`
- `total_input_tokens`, `total_output_tokens`, `total_reasoning_tokens`
- `rate_limit_errors` — count of 429-class responses
- `connection_errors` — count of dropped connections, timeouts, etc.

**Purpose:** post-hoc answer to "did we actually use the concurrency we configured?" If `max_inflight_llm=48` but `peak_inflight_llm=12`, we're bottlenecked somewhere other than the provider, and any tuned default is wrong until we fix the real bottleneck.

New module: `src/bitgn_contest_agent/bench/run_metrics.py`. Pure data collector + writer, no logic.

### 3.3 Web research step

Explicitly a research task, not code. Output is a markdown note at `docs/superpowers/research/cliproxyapi-rate-limits.md` with:

- Documented rate limits for cliproxyapi (if any in its README, docs, or source)
- Documented rate limits for Codex CLI auth (what cliproxyapi is proxying)
- User reports from GitHub issues, blog posts, or forums about hitting limits
- Observed reset behavior (how long does a throttled account stay throttled?)
- Sources cited

**Why it matters:** findings inform the burst test's starting concurrency and cooldown length. If users report 30-second cooldowns, 5 minutes is cautious but acceptable. If users report 20-minute lockouts, we rethink the burst strategy before running it.

**Fallback:** if research returns nothing useful, the burst test proceeds with a conservative ladder (start at 4, slower steps). The gap in knowledge is documented in the research note itself.

### 3.4 Synthetic burst test harness

Standalone script at `scripts/burst_test.py`. **Not** part of `run-benchmark`; a separate diagnostic tool.

**Flow:**

1. Wait 60 seconds before the first request (pre-cooldown, lets any prior cliproxyapi state settle).
2. For each level `L` in the ladder `[4, 8, 16, 32, 48, 64, 96]`:
   - Spin up `L` concurrent workers.
   - Each worker loops: fire a trivial chat/completions request (`"reply with the single word OK"`), record metadata, repeat.
   - Hold for 15 seconds of steady state at this level.
   - Check stop conditions.
   - If no stop condition triggered, step up to the next level.
3. Write `burst_report.json` incrementally (one JSON-lines record per call) so partial data survives a crash.
4. Print a markdown summary to terminal.

**Per-call recorded metadata:**

- `timestamp`, `concurrency_at_time` (level currently being tested), `latency_ms`
- `http_status`, `retry_after_seconds` (if present), `error_class` (if any)
- `input_tokens`, `output_tokens`

**Stop conditions (first to trigger wins):**

- `≥3 rate-limit errors in any rolling 10-second window`
- `p99 single-call latency > 45s` (computed over the last 30s of traffic)
- `Any connection drop`
- `Level 96 cleared cleanly` — ceiling is above anything Plan B / PROD needs
- `Floor safety: break happens below level 8` → raise `InsufficientHeadroomError`, abort, require human decision

**Per-call hard timeout:** 60 seconds. Prevents a single stuck request from hanging the burst.

**Secondary sanity burst (cross-check for token-based limits):** After the primary burst picks an operating point, run a 30-second steady burst at that operating point using **realistic** payloads (prompt length ~500 tokens, response length ~200 tokens). If this secondary burst triggers rate-limit errors that the primary (trivial-payload) burst did not, the ceiling is token-based (TPM) rather than request-based (RPM), and the operating point must be scaled down proportionally. The scaling factor is `trivial_tokens_per_call / realistic_tokens_per_call`. See §9 risk 2 for the residual concern this does not fully resolve.

**Module boundary:** standalone script with helpers in `src/bitgn_contest_agent/bench/burst.py`. The script is in `scripts/` because it is operational tooling, not library code — it can be run from environments where the full agent package is not installed.

**Cost envelope:** primary burst ≈ 100 requests × ~50 tokens each ≈ 5k tokens total. Secondary burst ≈ 30 requests × ~700 tokens each ≈ 21k tokens total. Under $0.50 on any reasonable rate card. The dominant cost is wall-clock (pre-cooldown 60s + burst 2–5min + post-cooldown 5min = ~7–10 min), not dollars.

### 3.5 Operating point selection

Pure function in `src/bitgn_contest_agent/bench/burst.py`:

```python
def pick_operating_point(report: BurstReport) -> ConcurrencyConfig: ...
```

**Rules:**

- Break at level `N` where `N >= 8`: `max_inflight_llm = floor(0.6 * N)`, `max_parallel_tasks = same`.
- Cleared through level 96: `max_inflight_llm = 48`, `max_parallel_tasks = 48` (not 96 — preserve headroom for token-heavy calls and provider variance).
- Break below level 8: raise `InsufficientHeadroomError`. Do not auto-commit a pathologically-low default; require a human decision.
- If the secondary sanity burst (§3.4) scales the operating point down: apply the scaling factor and document both the pre-scaling and post-scaling value in the operating-point artifact.

**Output:** a markdown file at `artifacts/bench/operating_point_<burst_report_hash>.md` containing:

- The chosen `max_inflight_llm` and `max_parallel_tasks`
- The rationale (which stop condition triggered, at which level, with which counts)
- The burst report hash (content-addressable traceability)
- The secondary sanity burst result, if applicable

**Commit trigger:** once the operating point is chosen, a dedicated commit updates `config.py` defaults and includes the operating-point markdown in the same diff. Reviewer can cross-check the rationale against the burst data.

**Table-driven unit tests:** synthetic burst reports with breaks at various levels, cleared reports, reports that break below level 8 — verify the function picks the expected config in each case.

### 3.6 Baseline gate run

After burst → operating-point commit → 5-minute mandatory cooldown → full bench run:

```
bitgn-agent run-benchmark \
  --benchmark bitgn/pac1-dev \
  --runs 3 \
  --max-parallel <tuned> \
  --max-inflight-llm <tuned> \
  --output artifacts/bench/<sha>_<ts>.json
```

**Pass criteria for the baseline (all must hold):**

- Median `pass_rate` across 3 runs ≥ current ratchet floor (22/43 ≈ 0.5116).
- Min `pass_rate` across 3 runs ≥ `floor - 2/43` (i.e., a single-run dip of up to two tasks below the floor is tolerated, because a ±9 pp single-run spread is the observed pattern).
- Run metrics show `peak_inflight_llm >= 0.8 * max_inflight_llm` (sanity: we're actually using the concurrency we configured).
- `rate_limit_errors` across all LLM calls ≤ 1 (we're comfortably below the provider's ceiling).

**Expected variance band at `--runs 3` on 43 tasks:** approximately ±3 pp around the median. This means Phase 3 improvements smaller than ~1 task may land in the "flat-but-principled" ratchet decision (§4.6) even when the rules are working. This is expected and not a flaw.

**Artifacts committed alongside the baseline bench:**

- `artifacts/bench/<sha>_<ts>.json` — the bench summary
- `artifacts/bench/<sha>_<ts>.run_metrics.json` — run metrics
- `artifacts/bench/<sha>_<ts>.triage.md` — baseline cluster breakdown
- `artifacts/bench/burst_report_<ts>.json` — burst data
- `artifacts/bench/operating_point_<burst_hash>.md` — operating-point rationale

### 3.7 v0.1.0 version bump

Atomic commit combining:

- `pyproject.toml` version → `0.1.0`
- `config.py` tuned parallelism defaults (if not already committed in §3.5)
- `artifacts/bench/README.md` updated to note the Phase 2 baseline and new defaults

Commit message explicitly names the burst report hash and the baseline artifact so the semver milestone is traceable. Git tag `v0.1.0` is applied in Phase 4 close, not here — Phase 4 ties the tag to the final closeout state, not mid-plan.

---

## 4. Phase 3 — Rules-only prompt rewrite

The only intentional behavior change in Plan B. Everything is designed around atomicity and measurability.

### 4.1 Feature branch

Phase 3 runs on a branch `plan-b/phase-3-rules` created from the post-Phase-2 commit in the Plan B worktree. It is merged back to the Plan B branch only if the ratchet criteria in §4.6 pass. This gives clean rollback (`git branch -D` on the feature branch) without touching the main-line history.

### 4.2 Rule content authoring → `prompts/runtime_rules.py`

New module: `src/bitgn_contest_agent/prompts/runtime_rules.py`. Contains six populated constants, each ~100–200 tokens. Concrete wording is drafted during writing-plans; the spec fixes only the *shape*.

**`AUTHORITY_HIERARCHY`** — paraphrase of the OpenAI Model Spec "Levels of Authority." Platform rules → Developer rules (AGENTS.md and nested AGENTS.md files) → User rules (task text) → Tool outputs. When two conflict, higher authority wins. Nested AGENTS.md refines its parent within its scope.

**`CONFLICT_RESOLUTION`** — when two instructions genuinely conflict and cannot be reconciled through scope narrowing or authority ranking, return `report_completion` with `OUTCOME_NONE_CLARIFICATION`. Direct replacement for the current "LAST resort" language in `prompts.py` (which actively discourages the outcome we want). Explicit framing: "being unable to choose is a valid answer; guessing is not."

**`SECURITY_POLICY`** — when a task instructs harmful, deceptive, or unauthorized actions (credential exfiltration, silent destruction, impersonation), return `OUTCOME_DENIED_SECURITY`. Read-only investigation of the suspicious instruction is permitted; mutations are not. Preserves the agent's ability to confirm suspicion without participating.

**`INBOX_IDENTITY_VERIFICATION`** — when processing inbox messages, verify sender email addresses against the claimed identity. Display names are not authoritative. If "Alice <attacker@evil.com>" conflicts with contact book "Alice <alice@company.com>", treat as suspicious: either clarify or deny. Directly addresses the t22 failure.

**`ERC3_PREPASS`** — for tasks that identify entities by human-readable fields (name, email), before executing any mutating action verify the entity exists and has the claimed properties. Prevents acting on spoofed or misremembered targets. Addresses the t41–t43 failure family.

**`TEMPORAL_GROUNDING`** — for any "N days ago" or date-relative reasoning, call the PCM `context()` tool and use the returned `unix_time` or RFC 3339 `time` field as the anchor. Do not guess the current date from training data, task text, or file timestamps. Addresses the t43 failure.

The module exports one helper:

```python
def all_rules() -> str:
    """Return all runtime rules concatenated in canonical order."""
```

The canonical order is: authority hierarchy → conflict resolution → security policy → inbox identity verification → ERC3 pre-pass → temporal grounding. Ordered from most general to most specific so the model encounters framing before rules.

### 4.3 Prompt builder wiring

Modify `src/bitgn_contest_agent/prompts.py` to insert `runtime_rules.all_rules()` output at a fixed location: **after the role/mission section and before the tool descriptions.**

**Simultaneous removal:** the existing "LAST resort" paragraph discouraging `OUTCOME_NONE_CLARIFICATION` is deleted. Not softened — deleted.

**Boundary:** the `NextStep` Pydantic schema is unchanged. The LLM call path is unchanged. Only the text of the system prompt changes.

### 4.4 `AGENTS.md` prune

In the same commit: remove the runtime-rule blocks from `AGENTS.md`. What remains is purely developer workflow — git discipline, how to run tests, how to structure PRs, how to regenerate artifacts, worktree conventions.

Post-prune, `AGENTS.md` and `runtime_rules.py` have zero content overlap. `AGENTS.md` is for humans reading the repo; `runtime_rules.py` is for the agent at runtime.

### 4.5 Atomic-commit discipline and wiring assertion

**One commit contains all of the above:** populated `runtime_rules.py`, wired-up `prompts.py`, pruned `AGENTS.md`. Never split into multiple PRs — partial states are worse than either old or new full state.

**Wiring assertion test (prevents silent-rules bug):** a new unit test in `tests/test_prompts.py` that builds the system prompt string and asserts a known substring from each rule category's first sentence is present. When `runtime_rules.py` or `prompts.py` is touched, this test runs. If wiring breaks — a rename, a missing import, a cached-prompt bug — the test fails immediately, long before a gate run would catch the same bug via the much slower divergence-counter route.

### 4.6 Measurement and ratchet decision

**Execution:**

```
1. On plan-b/phase-3-rules branch, run:
   bitgn-agent run-benchmark \
     --benchmark bitgn/pac1-dev \
     --runs 3 \
     --max-parallel <tuned> \
     --max-inflight-llm <tuned> \
     --output artifacts/bench/<sha>_<ts>.json

2. Run triage diff:
   bitgn-agent triage \
     --before artifacts/bench/<phase2-baseline>.json \
     --after  artifacts/bench/<phase3-result>.json \
     > artifacts/bench/triage_phase2_vs_phase3.md

3. Read divergence deltas from the schema v1.1 summary fields.
```

**Three pre-committed outcomes:**

**(A) Improved — commit new floor and merge.** All of the following must hold:

- Median `pass_rate` across 3 runs strictly greater than current floor (22/43) by at least 1 task (≈2.3 pp)
- 95% CI lower bound ≥ old median
- `overall.divergence_count` dropped from Phase 2 baseline (sanity check that rules actually fired)
- Triage diff shows the inbox cluster shrinking (the explicitly-targeted failure family)

Merge the feature branch to Plan B worktree. Update `artifacts/bench/README.md` and `project_bench_ratchet.md` memory with the new floor.

**(B) Flat but principled — no floor change, still merge.** All of the following must hold:

- Median `pass_rate` within the variance band of the old floor (±3 pp, explicitly)
- `overall.divergence_count` dropped meaningfully (at least 20% reduction)
- At least one failure cluster moved favorably in the triage diff (inbox, wrong_action, or false_refusal)
- No cluster grew significantly (no regression in unrelated failure modes)

Interpretation: the rules landed but the grader on `pac1-dev` is not rewarding them on this specific benchmark. The change is still defensible for PROD and future benchmarks. Merge the feature branch. The ratchet floor stays at 22/43. The close-out report (§5.1) documents the flat-but-principled call and why.

**(C) Regressed — do not merge, investigate.** Any of the following:

- Median `pass_rate` below floor
- 95% CI lower bound significantly below old median (more than 2 tasks)
- Triage diff shows an unexpected cluster growing (e.g., timeout cluster doubled)
- Prompt length inflation > 30% (rules cost token budget we can't afford)

Do not merge. Read the triage diff to localize the regression. If it is over-clarification on previously-passing tasks, iterate on specific rule wording on the feature branch and re-measure. If it is something unexpected, abandon the feature branch, delete it, and file a bug.

### 4.7 Risks to watch for during Phase 3

Flagged so reviewers know what to look at in the triage diff and run metrics:

- **Over-clarification regression.** The new conflict-resolution rule could make the agent bail on ambiguities that previously resolved fine. Watch for previously-passing tasks now terminating with `OUTCOME_NONE_CLARIFICATION`.
- **ERC3 pre-pass latency.** Extra verification steps add LLM calls per task → per-task wall-clock rises → timeout cluster could grow. Watch the timeout cluster delta and per-task median step counts.
- **Inbox over-blocking.** Identity verification could flag legitimate tasks with informal contact formats. Watch the inbox cluster for false positives (previously-passing inbox tasks now failing).
- **Prompt length inflation.** Adding ~800 tokens of rules to every LLM call raises per-call token cost. Watch `run_metrics.json` total token counts. If it jumps by >30%, we're paying more than the rule payload should cost — flag for B-prime compression.

Each has a concrete signal in metrics already collected by Phase 1's instrumentation; no new instrumentation needed for Phase 3.

---

## 5. Phase 4 — Close Plan B

Four discrete actions, no new code.

### 5.1 Close-out report

Markdown file at `docs/superpowers/reports/2026-04-XX-plan-b-closeout.md` (date set when Phase 4 lands). Contents:

- What shipped per phase, with commit SHAs
- Phase 2 burst report summary: break point (or "cleared to 96"), chosen operating point, rationale, secondary-sanity-burst result
- Phase 2 baseline numbers: median `pass_rate`, CI band, peak inflight, token totals
- Phase 3 triage diff: cluster deltas before → after, divergence delta
- Ratchet decision: improved / flat-but-principled / regressed (with the pre-committed criteria that triggered it)
- What surprised us — unexpected results, rule interactions, cost shifts, anything we would have bet against
- Explicit "deferred to B-prime" list

### 5.2 Ratchet update (conditional)

Conditional on Phase 3 outcome:

- **(A) Improved** — update `artifacts/bench/README.md` with the new floor commit and SHA; update `project_bench_ratchet.md` memory file with new numbers and date.
- **(B) Flat but principled** — leave the floor at 22/43. Add a section to `artifacts/bench/README.md` explaining that Phase 3 shipped without moving the floor, with a link to the close-out report.
- **(C) Regressed** — does not reach Phase 4 (the feature branch is not merged).

### 5.3 B-prime issue

GitHub issue titled `"Plan B-prime: Point 7 SDK upgrade + tool-usage prompt rewrite"`. Body enumerates B-prime scope:

- Vendor new `.proto` files under `bitgn-local-sdk/proto/`
- Add `scripts/regen.sh` for one-command future schema bumps (grpcio-tools + protoc-gen-connectrpc against `bitgn-local-sdk/proto/`)
- Regenerate `_pb2.py` / `_pb2.pyi`; `_connect.py` stays bit-identical (proven in Plan B spike)
- Bump `bitgn-local-sdk` version
- Extend `adapter/pcm.py`: `Read.number/start_line/end_line`, `Write.start_line/end_line`, `Tree.level`, `Context.time`
- Sanity check on a live playground trial: one ranged read, one ranged write, one depth-limited tree
- Rewrite prompt guidance + NextStep examples so the model reaches for bounded reads, ranged writes, depth-limited tree, and line numbers
- Measure against Plan B's new ratchet floor (whatever §5.2 committed)
- PROD run (post-2026-04-10T13:00+02:00 — constraint already lifted by the time B-prime lands)

The issue references the Phase 2 baseline artifact and the Phase 3 ratchet decision so B-prime's measurement baseline is unambiguous.

### 5.4 Version tag

Tag the close commit `v0.1.0` in git. This is the user-facing milestone marking "iteration loop proven."

### 5.5 Worktree hygiene

Before Phase 4 close, the Plan B worktree is rebased onto the current `main` if main has moved during Plan B execution. Rationale: long-running worktrees accumulate drift. Rebasing at close (rather than merging from main periodically during Plan B) keeps the Plan B commit history linear and reviewable, and concentrates any merge conflict resolution at one predictable point. If rebase surfaces non-trivial conflicts, those are resolved in the Plan B worktree before the v0.1.0 tag lands.

---

## 6. End-to-end data flow

```
Phase 1
  (no bench artifacts — only uncommitted smoke runs during CI)
  │
  ▼
Phase 2
  docs/superpowers/research/cliproxyapi-rate-limits.md
    │
    ▼
  burst_report_<ts>.json ──► operating_point_<hash>.md
    │                              │
    │                              ▼
    │                         config.py new defaults
    │                              │
    ▼                              ▼
  <phase2-sha>_<ts>.json    (schema v1.1 bench summary)
    + <phase2-sha>_<ts>.run_metrics.json
    + <phase2-sha>_<ts>.triage.md
  │
  ▼
Phase 3 (on plan-b/phase-3-rules feature branch)
  <phase3-sha>_<ts>.json
    + <phase3-sha>_<ts>.run_metrics.json
    │
    ▼
  triage_phase2_vs_phase3.md       (cluster diff)
    │
    ▼
  ratchet decision (A / B / C)
    │
    ▼
Phase 4 (main Plan B branch, post-merge)
  closeout report   ◄── references all of the above
  ratchet floor update (memory + bench README)  — conditional on (A)
  B-prime GitHub issue
  v0.1.0 tag
```

**Directory layout at Plan B close:**

```
artifacts/bench/
  README.md                                    # updated with new (or same) floor
  1623b40_20260410T181832Z.json                # Plan A floor (retained)
  <phase2-sha>_<ts>.json                       # Phase 2 baseline
  <phase2-sha>_<ts>.run_metrics.json
  <phase2-sha>_<ts>.triage.md
  <phase3-sha>_<ts>.json                       # Phase 3 result (if merged)
  <phase3-sha>_<ts>.run_metrics.json
  burst_report_<ts>.json
  operating_point_<burst-hash>.md
  triage_phase2_vs_phase3.md

docs/superpowers/
  research/cliproxyapi-rate-limits.md         # Phase 2 research note
  specs/2026-04-11-plan-b-design.md           # THIS spec
  plans/2026-04-11-plan-b.md                  # to be written after brainstorming
  reports/2026-04-XX-plan-b-closeout.md       # Phase 4
```

---

## 7. Error handling

Phase-by-phase catalog of edge cases we can name now, with documented responses.

### 7.1 Phase 1

- **Smoke test fails on a Phase 1 PR** — block the PR, investigate, do not waive. The failure is the signal.
- **LLM client swallows token metadata** — expand Phase 1 scope by one PR to plumb token counts through the client response handler. Still inside the plumbing-only invariant because LLM call semantics do not change, only what is persisted from the response.
- **Divergence counter matches false positives** — accepted. It is a heuristic tertiary signal. Document the known false-positive patterns in the module docstring.
- **bench_summary v1.1 breaks an existing consumer** — caught by the round-trip test in §2.1. Fix forward rather than roll back the schema bump, because the bump is additive-only.

### 7.2 Phase 2

- **Web research returns nothing useful about cliproxyapi limits** — proceed with conservative burst (start at 4, slow step-up). Document the knowledge gap in the research note.
- **Burst test crashes mid-run** — partial data is preserved in `burst_report.json` via incremental append. Rerun after a full cooldown (5 min + 60s pre-cooldown = ~6 min total). Do not attempt to resume the crashed burst; start over so the ladder counts are clean.
- **Burst breaks below level 8** — raise `InsufficientHeadroomError`, halt Phase 2, require human decision. Likely causes: cliproxyapi is degraded, wrong auth, network issue. Do not auto-commit a pathologically-low default.
- **Primary burst clears cleanly but secondary sanity burst triggers rate limits** — rate limit is token-based. Scale operating point down by the token ratio (§3.4, §3.5). Document both pre-scaling and post-scaling values.
- **Baseline regresses the existing 22/43 floor** — first check whether the regression is within the variance band (single-run dip vs median drop). If the median is below, try dropping concurrency by 20% and re-running before accepting the regression — the burst-derived ceiling may be too aggressive for real task workloads, which are more token-heavy than "reply OK."
- **Peak inflight < 80% of configured max** — something other than the provider is bottlenecked (thread pool sizing, GIL, I/O wait). Flag in run metrics, investigate before Phase 3. Phase 3 measurements will be meaningless if we are not actually parallel.
- **Rate-limit errors > 1 across the baseline run** — operating point is too close to the ceiling. Drop by 20% and re-run the baseline once. If it still triggers rate limits, re-run the burst test; the ceiling may have shifted (provider-side changes).

### 7.3 Phase 3

- **Feature branch gate regresses** — do not merge. Read the triage diff. If it is over-clarification on previously-passing tasks, iterate rule wording on the feature branch. If it is a completely unexpected cluster growing, abandon the branch and file a bug.
- **Feature branch gate flat AND divergence counter unchanged** — the rules are not firing. This is almost always a wiring bug, not a rule-content bug. The wiring assertion test (§4.5) should have caught it; if it did not, first investigate why the test passed while the rules are not reaching the LLM. Do not merge.
- **Feature branch gate improves but prompt length inflation > 30%** — merge, but document the token cost in the close-out report. Flag for B-prime: the rules may need compression or one-shot summarization.
- **All Phase 3 attempts regress regardless of wording iteration** — abandon Phase 3, close Plan B with the "regressed" outcome, report explicitly says "Plan B infra proven; rules approach did not land on pac1-dev; next improvement in B-prime with tool-usage rewrite."

### 7.4 Phase 4

- **No ratchet improvement to record** — still close Plan B. The iteration loop is proven by Phases 1–2 infra regardless of whether Phase 3 moved the scorecard. Report explicitly says "flat; loop proven by infra; rules shipped on defensibility grounds; next improvement in B-prime."
- **Rebase surfaces non-trivial merge conflicts from main drift** — resolve in the Plan B worktree before tagging v0.1.0. If resolution is contentious or changes behavior, run smoke tests post-rebase before tagging.

---

## 8. Testing approach

### 8.1 Unit tests (fast, run on every Phase 1 PR)

- **`aggregate.py`** — deterministic percentile bootstrap with fixed RNG seed; variance stats against hand-computed inputs; backward-compat case with N=1 runs; round-trip against the existing Plan A baseline artifact.
- **`triage.py`** — fixture traces with known clusters (including the t21/t22/t43 patterns); verify classification is stable across the clustering rules order.
- **`divergence.py`** — fixture traces from the existing t21/t22 logs (which should register as divergent); negative fixtures from tasks where scratchpad mentions "conflict" or "both" benignly (should not register).
- **`run_metrics.py`** — injected timing; verify gauge sampling cadence and histogram bucket assignment.
- **`burst.py`** — mock `Backend` Protocol with scripted response patterns (clean, rate-limited, slow, disconnected); verify each stop condition triggers exactly when expected; verify incremental writes survive a simulated crash.
- **`pick_operating_point`** — table-driven: burst report with break at level N → expected config; cleared report → default 48; break below 8 → raises `InsufficientHeadroomError`; secondary sanity burst scaling factor applied correctly.
- **`tests/test_prompts.py` wiring assertion** (Phase 3) — builds the system prompt string and asserts a known substring from each rule category's first sentence is present. This is the single most important test in Phase 3 because it is the only non-heuristic guard against silent rule-wiring bugs.

### 8.2 Integration tests (slower, run on Phase 1 PRs and before Phase 2/3 baselines)

- **Smoke test subset** — `t02, t42, t41, t15, t43` end-to-end against live cliproxyapi with `--max-parallel 5 --max-inflight-llm 8`. Wall-clock budget 180s. Pass criteria: `t02/t42/t41` pass, `t15/t43` fail, wall-clock within budget.

### 8.3 Manual verification (Phase 2 and Phase 3 close-out steps)

- **Burst report eyeball** — does the latency/RPS curve look sane? Is the break point clean or jittery? Does the operating-point rationale match what the data shows?
- **Triage diff eyeball** — are the cluster movements plausible? Did anything move that was not expected? Did anything that *should* have moved not move?
- **Random Phase 3 trace spot-check** — pick 3 post-Phase-3 traces from previously-failing inbox tasks and read them. Did the agent actually exercise the new rules? Is the scratchpad reasoning reflecting the authority hierarchy or inbox identity verification? If the rules are present in the prompt but the model is ignoring them, that is a prompt-engineering issue to note for B-prime.

### 8.4 Explicit non-goals

- **Rule content correctness via unit tests** — the "does this prompt actually work" question is answered by the gate, not by mock-LLM tests. Don't write brittle tests that mock the LLM and verify rule behavior.
- **cliproxyapi as a system under test** — treat it as a black box whose behavior we characterize empirically via burst, not whose correctness we validate.
- **End-to-end full-bench in CI** — bench runs are expensive and non-deterministic; they are a release-gate artifact, not a unit test. CI runs smoke tests, not gate runs.

---

## 9. Open risks carried into writing-plans

These are risks the spec acknowledges but does not fully resolve; writing-plans should surface them as explicit task-level concerns so the implementer has eyes on them:

1. **Divergence counter keyword list is empirically derived from two known traces (t21, t22).** If Phase 3's agent wording diverges from the training fixtures, the counter may silently stop firing. Mitigation: divergence is a tertiary signal only; primary signals are pass_rate and triage cluster movement.
2. **The secondary sanity burst assumes the rate limit scales linearly with token count.** This may not hold if cliproxyapi enforces both RPM and TPM independently, or if there is a separate per-minute quota. The fallback if the operating point proves unsafe on real runs is in §7.2.
3. **Phase 3 atomic commit is fragile to the wiring bug class.** The wiring assertion test (§4.5) covers the expected bug mode; it cannot cover unanticipated ones (e.g., prompt caching at the backend layer that persists across tests).
4. **`--runs 3` on 43 tasks gives a wide CI band (~±3 pp).** Small Phase 3 improvements will land in flat-but-principled territory. This is a structural limit of the benchmark size, not a flaw in the plan; it is called out in §3.6 and §4.6 so neither the author nor future reviewers are surprised.
5. **The 5-minute post-burst cooldown is conservative, not empirical.** If research (§3.3) returns concrete reset-behavior data, Phase 2 may be able to shorten it. If it does not, 5 minutes is the safe default.
6. **`max_parallel_tasks` and `max_inflight_llm` are tuned to the same value in §3.5.** In principle they are separable; in practice they are close enough that tuning them identically is fine for Plan B. If B-prime measurements suggest they should diverge, that's a B-prime concern.
7. **Prompt length inflation from ~800 new tokens of rules raises per-call cost.** The inflation is bounded and acceptable for Plan B; if PROD economics demand compression, that's a B-prime concern.

---

## 10. Success criteria for Plan B

Plan B is considered successful if **all of the following hold at close:**

1. `bench_summary.json` schema v1.1 is merged, round-trips v1.0, and is in use by the gate.
2. `bitgn-agent triage` exists as a CLI subcommand and produces diff-mode output against Phase 2 / Phase 3 artifacts.
3. The burst test has been run at least once and has produced a committed operating point under `artifacts/bench/`.
4. The Phase 2 baseline bench run at tuned parallelism is committed to `artifacts/bench/` and passes the §3.6 pass criteria.
5. Phase 3 has been attempted (rules landed on the feature branch and measured) and a pre-committed ratchet decision (A / B / C) has been recorded in the close-out report.
6. `v0.1.0` git tag is applied to the final close commit.
7. The B-prime follow-up issue is opened with explicit scope and baseline references.

Note that none of these conditions require `pass_rate` to have improved. Plan B's primary goal is loop proof; behavior improvement is a secondary win that may or may not land on `pac1-dev` specifically. A Phase 3 that lands in flat-but-principled is still a successful Plan B.

---

## Appendix A — Component-to-file map

| Component | Files created / modified | Phase |
|---|---|---|
| Schema v1.1 round-trip | `src/bitgn_contest_agent/bench/summary.py` (or equivalent) + `tests/test_summary_schema.py` | 1 |
| Multi-run aggregator | `src/bitgn_contest_agent/bench/aggregate.py` + `tests/test_aggregate.py` | 1 |
| Failure triage | `src/bitgn_contest_agent/bench/triage.py` + CLI wire-up in `src/bitgn_contest_agent/cli.py` + `tests/test_triage.py` | 1 |
| Token + harness_url capture | `src/bitgn_contest_agent/trace_writer.py` (modified) + `tests/test_trace_writer.py` (expanded) | 1 |
| Divergence counter | `src/bitgn_contest_agent/bench/divergence.py` + `tests/test_divergence.py` | 1 |
| Smoke subset | `src/bitgn_contest_agent/bench/smoke.py` + flag wire-up in CLI | 1 |
| `max_inflight_llm` semaphore | `src/bitgn_contest_agent/agent.py` or `orchestrator.py` (wherever `backend.call` is invoked) + `tests/test_semaphore.py` | 2 |
| Run metrics | `src/bitgn_contest_agent/bench/run_metrics.py` + `tests/test_run_metrics.py` | 2 |
| Web research note | `docs/superpowers/research/cliproxyapi-rate-limits.md` | 2 |
| Burst harness | `scripts/burst_test.py` + `src/bitgn_contest_agent/bench/burst.py` + `tests/test_burst.py` | 2 |
| Operating point picker | `src/bitgn_contest_agent/bench/burst.py` (same file) + `tests/test_burst.py` | 2 |
| Baseline run artifacts | `artifacts/bench/<phase2-sha>_<ts>.json` + sidecars | 2 |
| v0.1.0 bump | `pyproject.toml`, `config.py`, `artifacts/bench/README.md` | 2 |
| Runtime rules module | `src/bitgn_contest_agent/prompts/runtime_rules.py` + `tests/test_runtime_rules.py` | 3 |
| Prompt builder wire-up | `src/bitgn_contest_agent/prompts.py` (modified) + wiring assertion in `tests/test_prompts.py` | 3 |
| `AGENTS.md` prune | `AGENTS.md` (runtime rule sections removed) | 3 |
| Phase 3 bench artifacts | `artifacts/bench/<phase3-sha>_<ts>.json` + sidecars + `triage_phase2_vs_phase3.md` | 3 |
| Close-out report | `docs/superpowers/reports/2026-04-XX-plan-b-closeout.md` | 4 |
| Ratchet update | `artifacts/bench/README.md`, `project_bench_ratchet.md` memory | 4 |
| v0.1.0 tag | git tag | 4 |
| B-prime issue | GitHub issue | 4 |

---

*End of Plan B design spec.*
