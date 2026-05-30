# ECOM1-PROD Precision Improvements — Spec & Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This doc is a fresh-context handoff.** It is self-contained: read §0–§3 fully before touching code. The hard-won findings here came from a graded PROD run + a 30-agent trace-analysis workflow + 4 live `/bin/jq` probes on 2026-05-30; that context is being cleared, so this file (plus the memory entries it references) is the source of truth.

**Goal:** Raise the ecom1-prod agent from a measured **mean 0.7863 (73/100 pass@1.0)** toward ~0.90+ by fixing **precision bugs in *answered* tasks** (the real headroom), using deterministic filesystem/`/bin/jq` extraction — without regressing the ~73 passing tasks or the ~29 correct security refusals.

**Architecture:** ReAct agent (`src/bitgn_contest_agent/`) driving the BitGN ECOM runtime. PROD has **no `/bin/sql`** (data lives in the `/proc` filesystem) and now ships a restricted **`/bin/jq`**. Improvements are additive, env-gated levers (`BITGN_USE_*`) validated by **local A/B against `LocalEcomClient` snapshots before any PROD run**.

**Tech Stack:** Python 3.12, `uv`, pytest; gpt-5.4 via linkapi (`scripts/use_provider.sh linkapi`); BitGN harness SDK (`bitgn.*`).

---

## §0. Current State — what is already DONE (do not redo)

- **Contest submission tagged.** `v0.1.152` (annotated tag on commit `c39e546`) marks the contest agent state. It is pushed to `origin/session/53-task-push`. **Any commit after this tag is post-submission and does NOT count toward the contest.** Do work on a branch off `c39e546`/main, not on the tag.
- **Local `/bin/jq` emulation built + tested but NOT yet committed.** In the **main worktree** (`/home/claude-developer/bitgn-ecom`), uncommitted:
  - `src/bitgn_contest_agent/local/ecom_client.py` — added `_JQ_BIN_PATHS`, `_JQ_BANNER`, `_eval_jq_filter()`, `_jq_format_value()`, `_exec_jq()`, dispatch wiring, `/bin/jq` in `_BIN_STUB_PATHS`. Faithful to the probed PROD grammar.
  - `tests/local/test_jq_exec.py` — 35 passing tests pinning the grammar. `uv run python -m pytest tests/local/test_jq_exec.py -q` → all pass; full local suite green.
  - `scripts/probe_jq*.py` (4 probes) + `artifacts/prod_explore/jq_probe*.json` (raw PROD evidence).
  - **First implementation task is to commit these** (Task 1.1).
- **A scored PROD run exists.** `run-22RyWEqB49qyKD4bM1Kux1fnP` (v0.1.152, gpt-5.4, linkapi, `--max-parallel 16 --max-inflight-llm 32`). Scores fetched to `artifacts/bench/SCORES_run-22RyWE_v0.1.152_release.json`; outcome×score cross-ref in `artifacts/bench/XREF_v0.1.152_outcome_score.txt`.
- **A worktree exists** at `/home/claude-developer/bitgn-ecom-rel-v0.1.152` (detached @ `c39e546`). Safe to `git worktree remove` when done.
- **Memory written:** `project_ecom_prod_jq_contract`, `project_ecom_prod_proc_namespaces` (+ this session adds the ground-truth reframing — see §1).

---

## §1. Ground-Truth Findings (THE REFRAMING — read this)

The blind-run analysis assumed the headroom was the 37 non-OK refusals. **The graded run proves the opposite.** Categorizing all 100 tasks by `(reported outcome) × (real score)`:

| Category | Count | Meaning |
|---|---|---|
| **PASS** (score ≥ 0.999) | 73 | correct |
| **OK_WRONG** (OUTCOME_OK, score < 1.0) | **21** | answered but **wrong/partial** → the real headroom |
| **REFUSAL_FAIL** (refused/abstained, score 0) | **6** | gave up but should have answered |
| refusals that PASSED | 29 | **correct** security/policy denials → **LEAVE ALONE** |

**Implication: the dominant defect is precision on tasks the agent already attempts, not over-refusal.** Refusal policy is mostly right (29/35 refusals scored 1.0).

### Critical caveat: ecom1-prod RE-INSTANTIATES per run
Task IDs are **templates**, instantiated with fresh concrete values each run. Proven: `t002` was "bosch gws 1400 / 6 units" in one run and "stihl hsa 50 / 10 units" in another; some tasks (`t005`) are stable, many are not. **Consequences:**
- 0.7863 / 73-100 is **one sample**; expect run-to-run variance (consistent with the `variance_is_reliability` memory).
- **Fixes MUST target task FAMILIES / intents / mechanisms, never specific task IDs.** A per-task-ID enforcer is worthless.
- The trace-analysis workflow diagnosed a *different* run's instantiation than the graded run, so use it for **family-level mechanism insight**, not per-task truth.

### `/bin/jq` is a restricted custom binary (probed live — memory `project_ecom_prod_jq_contract`)
- Usage `jq [-r|--raw-output] <filter> [path|-]`; banner line `PowerTools E-Commerce OS jq\n` always prefixes stdout; **only `-r`** supported.
- **Supported:** `.`, `.a.b.c` nested, `.arr[i]`, `.arr[i].field`, `.arr[]`, `.arr[].field` (iterate, newline-sep), `keys`. Reads stdin by default; robust pattern = `read` file → pipe content via stdin.
- **Hard-fails:** pipes `|`, `select()`, `has()`, `[...]` collect, arithmetic `+ - *`, string interp.
- **Silently returns `null` (exit 0) — TRAP:** `length`, `type`, `tostring`, `// default`, comparisons. **jq CANNOT count or compute.**
- **Therefore jq helps: field extraction + ref enumeration (`.arr[].field`). jq does NOT help: the count family.**

### `/proc` namespaces on prod differ from the prompt (memory `project_ecom_prod_proc_namespaces`)
`/AGENTS.MD` on ecom1-prod: stores=`/proc/locations`, employees=`/proc/staff`, baskets=`/proc/carts`, payments=`/proc/payment-ledger`, returns=`/proc/return-workflows`, catalogue=`/proc/catalog`. The prompt's flat examples (`/proc/payments/<id>.json`) 404 on prod. Fix must be **path-agnostic** (force reliance on AGENTS.MD), because ecom1-dev likely still uses the old paths and the prompt serves both.

---

## §2. Headroom by Family (prioritize by points × safety)

Measured against `run-22RyWEqB49qyKD4bM1Kux1fnP`:

| Family | Tasks (this sample) | Recoverable pts | Symptom | jq helps? | Risk |
|---|---|---|---|---|---|
| **count / availability** | t002,t005,t025,t045,t062,t065 | **+6.00** | wrong COUNT ("how many SKUs ≥N units", "do you have N of X") | ❌ (can't count) | MED |
| **REFUSAL_FAIL** | t010,t013,t026,t052,t072,t099 | **+6.00** | abstained/denied but should answer; incl. cross-cust denials scoring 0 | partial | HIGH (don't loosen real refusals) |
| **sku / product-resolution** | t007,t027,t046,t067 | **+4.00** | wrong/over-clarified SKU under price/spec filter | ✅ (extract specs) | MED |
| **ocr-receipt** | t003,t063 | **+2.00** | wrong basket from OCR upload | partial | MED |
| **cleanup** | t060 | **+1.00** | `/tmp/cleanup-*` file deletion wrong | ❌ | MED |
| **checkout / 3ds** | t087 | **+1.00** | 3DS-recovery checkout answered wrong | ✅ (read gate field) | MED |
| **dispatch-wave** | t004,t014,t024,t044,t064 | **+0.89** | partial ~0.82 (missing refs/elements) | ✅ (ref enum) | LOW |
| **archive / risk-ops** | t055,t075 | **+0.48** | partial ~0.76 (fraud component) | ✅ | MED |

**Total recoverable ≈ +21.4 → ceiling ≈ 1.00.** Realistic first target: count + sku + ocr + cleanup + dispatch ≈ +13 → ~0.90.

**Recommended order:** (1) dispatch-wave — likely a free win by enabling an existing gated lever; (2) count/availability — biggest cluster; (3) sku-resolution; (4) ocr + cleanup + 3ds; (5) REFUSAL_FAIL (investigate first — risky); (6) archive.

---

## §3. Hard Constraints & Methodology (NON-NEGOTIABLE)

1. **Local A/B before every PROD run.** A PROD run costs ~$15 and run slots (10/30min). Validate each lever with `scripts/local_bench.py` against snapshots first. (memory `feedback_local_ab_before_prod`, `feedback_local_first`.)
2. **Enforcers ADD `grounding_refs` (union); they NEVER rewrite the model's answer.** Rewriting numeric/SKU answers has repeatedly regressed (memory `project_ecom_v108_deterministic_42_42`). SKU/count "overrides" that pick a value are proven net-negative on under-spec multi-variant tasks (`BITGN_USE_COUNT_OVERRIDE` was reverted).
3. **Every new lever is env-gated `BITGN_USE_*`, default OFF**, shipped only after a clean local A/B (no regression vs the 73 passing + 29 correct refusals).
4. **Never touch the correct-refusal path.** 29 refusals scored 1.0. Security/injection/cross-owner/3DS-bypass/discount-fraud denials are correct.
5. **Family-level, not task-ID-level** (see re-instantiation caveat).
6. **Prompt changes can hurt recall** (memory `feedback_pre_submit_checklist_hurts_recall`). A/B prompt edits; don't bundle precision warnings with recall-bound tasks.

---

## §4. Phase 0 — Enabling Infrastructure (do FIRST; blocks faithful A/B)

### Task 0.1 — Prod-namespace local mode (`BITGN_LOCAL_PROD_PATHS`)

**Why:** `LocalEcomClient` synthesizes reads for the *dev* paths (`/proc/payments/`, `/proc/baskets/`). To A/B any prod fix (namespace, refund, checkout) faithfully, the local client must also answer the *prod* paths (`/proc/payment-ledger/`, `/proc/carts/`, `/proc/locations/`, `/proc/staff/`, `/proc/return-workflows/`). Mirrors the existing `BITGN_LOCAL_SQL_UNAVAILABLE` mode (commit `6d18051`).

**Files:**
- Modify: `src/bitgn_contest_agent/local/ecom_client.py` (the `read()` synth block, ~lines 467–493; the `_synth_row_read` calls)
- Test: `tests/local/test_prod_namespaces.py` (create)

- [ ] **Step 1: Read the current synth block** — Run: `sed -n '460,520p' src/bitgn_contest_agent/local/ecom_client.py`. Confirm the `path.startswith("/proc/payments/")` / `/proc/baskets/` branches and the `_synth_row_read(path, table, idcol)` signature.

- [ ] **Step 2: Write failing test** in `tests/local/test_prod_namespaces.py`:
```python
import os
from types import SimpleNamespace
import pytest
from bitgn_contest_agent.local.ecom_client import LocalEcomClient

def _req(**kw): return SimpleNamespace(**kw)

@pytest.fixture
def client(fixture_workspace):  # reuses conftest fixture_workspace with a catalogue.db
    return LocalEcomClient(fixture_workspace)

def test_prod_payment_ledger_path_synthesizes(client, monkeypatch):
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    # a payment id known to exist in the fixture payment table
    r = client.read(_req(path="/proc/payment-ledger/pay-0001.json"))
    assert r.content_type == "application/json"
    assert r.content and "pay-0001" in r.content

def test_dev_path_still_works_when_flag_off(client):
    r = client.read(_req(path="/proc/payments/pay-0001.json"))
    assert r.content
```
(Adjust `pay-0001` to an id present in `tests/local/fixtures` catalogue; inspect with `sqlite3` first.)

- [ ] **Step 3: Run test, verify FAIL** — Run: `uv run python -m pytest tests/local/test_prod_namespaces.py -q`. Expected: FAIL (prod path 404s).

- [ ] **Step 4: Implement** — in `read()`, when `os.environ.get("BITGN_LOCAL_PROD_PATHS")=="1"`, add prod-path branches alongside the dev ones:
```python
elif path.startswith("/proc/payment-ledger/") and path.endswith(".json"):
    synth = self._synth_row_read(path, "payment_transactions", "payment_id")
elif path.startswith("/proc/carts/") and path.endswith(".json"):
    synth = self._synth_row_read(path, "shopping_baskets", "basket_id")
```
Keep the dev branches unconditional so flag-off behavior is unchanged. (Add `/proc/locations/`, `/proc/staff/`, `/proc/return-workflows/` as their backing tables are confirmed — verify table names via `scripts/deep_extract_trial.py` output or a live probe.)

- [ ] **Step 5: Run tests, verify PASS** — Run: `uv run python -m pytest tests/local/test_prod_namespaces.py tests/local/ -q`. Expected: PASS, full local suite green.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(local): BITGN_LOCAL_PROD_PATHS mode mirrors prod /proc namespaces for faithful A/B"` (include the Co-Authored-By trailer).

### Task 0.2 — Commit the already-built `/bin/jq` emulation

**Files:** `src/bitgn_contest_agent/local/ecom_client.py`, `tests/local/test_jq_exec.py`, `scripts/probe_jq*.py`.

- [ ] **Step 1: Verify tests pass** — Run: `uv run python -m pytest tests/local/test_jq_exec.py -q`. Expected: 35 passed.
- [ ] **Step 2: Verify full local suite** — Run: `uv run python -m pytest tests/local/ -q`. Expected: green.
- [ ] **Step 3: Commit** — `git add src/bitgn_contest_agent/local/ecom_client.py tests/local/test_jq_exec.py scripts/probe_jq*.py && git commit -m "feat(local): faithful PowerTools-OS /bin/jq emulation + probes + tests"`.

---

## §5. Phase 1 — Wire `/bin/jq` into the agent (the original request)

**Decision (user):** make `/bin/jq` the primary query path. **Scope correctly:** jq replaces SQL for **field extraction + ref enumeration**, NOT counting/compute (it can't). Frame as an accelerator, not a hard dependency (fall back to `read` if jq absent).

### Task 1.1 — Add `/bin/jq` to the bins inventory + discipline in `prompts.py`
**Files:** Modify `src/bitgn_contest_agent/prompts.py` ("Catalogue / SQL discipline" section, ~lines 398–446).

- [ ] **Step 1:** Read the section — `sed -n '398,476p' src/bitgn_contest_agent/prompts.py`.
- [ ] **Step 2:** Add a `/bin/jq` entry to the bins list and a discipline block. Content to insert (verbatim guidance — only the WHITELISTED grammar, with the silent-null trap called out):
```
      /bin/jq        — deterministic JSON field extractor over /proc
                       records (the /bin/sql replacement). Contract:
                       `exec /bin/jq args=["-r","<filter>"]` with the
                       file CONTENT on stdin (read the file first, pipe
                       it in). Output has a banner first line — ignore it.
                       SUPPORTED filters ONLY: `.a`, `.a.b.c`, `.arr[i]`,
                       `.arr[i].field`, `.arr[]`, `.arr[].field`, `keys`.
                       Use it to (a) read ONE policy/status field before
                       deciding (e.g. a 3DS-recovery flag, a return
                       status, a discount cap), and (b) ENUMERATE refs
                       (`.lines[].sku`) to make grounding_refs complete.
                       DO NOT use pipes, select(), has(), arithmetic, or
                       length/type — they error or silently return null.
                       jq CANNOT count: to count, enumerate then count
                       the lines yourself. Cite the /proc FILE PATH you
                       read, never the jq output string.
```
- [ ] **Step 3:** Gate behind `BITGN_USE_JQ` (read in `config.py`; conditionally include the block) so the prompt change can be A/B'd in isolation. Follow the existing `BITGN_USE_*` pattern (`grep -n "BITGN_USE_" src/bitgn_contest_agent/config.py`).
- [ ] **Step 4:** Local A/B — see §6 "How to A/B". Expected: no regression on the passing set; watch for partial-credit lift on ref-enumeration tasks.
- [ ] **Step 5:** Commit only if A/B is clean.

---

## §6. Phase 2 — Per-Family Improvement Specs

> Each family is an independent sub-project. Per the writing-plans scope rule, **write a dedicated `docs/superpowers/plans/` sub-plan per family when you pick it up** (brainstorm → investigate current code → TDD plan). Below are the specs + concrete starting points so that planning is fast. Do them in the §2 recommended order.

### 2A. dispatch-wave (LOW risk, quick win) — +0.89
- **Symptom:** answered, partial ~0.82 across 5 tasks (`Plan the dispatch wave described in /ops/dispatch/wave-*/dispatch.md`).
- **Hypothesis:** a deterministic planner already exists and is **gated off**: `src/bitgn_contest_agent/dispatch_planner.py` + `BITGN_USE_DISPATCH_PLANNER` (commit `8e5412a`). Partial credit suggests the LLM's plan is close but misses elements/refs the deterministic planner would nail.
- **First steps:** Read `dispatch_planner.py`; find a dispatch snapshot (`artifacts/prod_explore/dispatch_wave_*` exists) or build one; local A/B with the flag ON vs OFF. If it lifts ~0.82→1.0 with no regression, ship gated-ON for prod.
- **Risk:** LOW (isolated, gated, additive).

### 2B. count / availability (biggest, +6.00) — MED
- **Symptom:** "how many SKUs have ≥N units at <store>", "do you have N of <product>" → wrong count (0.0). Store = `/proc/locations/<city>/...`; inventory `available_today` per (store, sku).
- **Root cause:** PROD has no `/bin/sql`; the LLM counts by eyeballing files and miscounts; the existing `count_rederive` (`BITGN_USE_REDERIVE_COUNT`) and `refless_count_override` use **SQL** (dead on prod — memory `project_ecom_count_completer_dead_in_prod`). **jq cannot count.**
- **Approach (additive, no answer-rewrite):** deterministic **filesystem** count — `find`/`list` the store inventory records, read each, apply the threshold predicate **in Python**, and use the result to (a) self-check/bounce the LLM's count on disagreement (like `count_rederive` bounces, never rewrites) and (b) add the per-SKU `/proc` paths as grounding_refs. Reuse the bounce-not-rewrite pattern from `src/bitgn_contest_agent/count_rederive.py`. **Open problem:** robustly enumerating the inventory set without SQL — investigate the prod inventory layout via a read-only probe (`scripts/probe_jq*.py` pattern) before designing.
- **Risk:** MED. Do NOT ship a value-rewriting override (proven net-negative). Bounce-on-disagreement + abstain-on-ambiguity only.

### 2C. sku / product-resolution (+4.00) — MED
- **Symptom:** "resolve <product> under EUR X", "8pc bosch cyl-9 ... metal cassette — does it exist", over-clarification or wrong pick.
- **Insight (workflow, SAFE_SHIP):** gpt-5.4 over-emits `OUTCOME_NONE_CLARIFICATION` when a qualifier token uniquely discriminates one candidate. **Prompt-only nudge:** when exactly ONE candidate matches a request token (e.g. "set", a capacity, a price bound) more specifically, prefer that single best match (OK) before clarifying; clarify only when ≥2 candidates genuinely tie. **Uniqueness-gated** so it never fires on truly-ambiguous instances (workflow t021/t061 = genuine ambiguity, LEAVE ALONE). jq helps extract per-variant spec fields for the price/spec filter.
- **Constraint:** LLM nudge only — **no deterministic SKU rewriter** (must guess on ties → converts correct CLR to wrong OK). Verified safe by the workflow (`SAFE_SHIP`, verdict isolated from refusal paths).
- **Risk:** MED (prompt recall sensitivity — A/B).

### 2D. ocr-receipt (+2.00), cleanup (+1.00), checkout/3ds (+1.00) — MED
- **ocr-receipt:** "buy the same basket as in the OCR upload" → read `/uploads/*_receipt_ocr.txt`, resolve each line to a SKU, build basket. Wrong basket = resolution error. Reuse SKU-resolution fix (2C) per line; jq enumerate basket lines for refs.
- **cleanup:** "clean out every temp file under `/tmp/cleanup-*`, delete all files [matching criteria]" → file-deletion task scored 0.0. Investigate the criteria parsing and the delete op; likely deletes wrong set or misses the criteria. Build a snapshot, A/B.
- **checkout/3ds:** "basket stuck on bank approval for payment pay-X" → read the payment's 3DS-recovery gate field (jq `.three_ds...` or the prod equivalent under `/proc/payment-ledger`), then conditionally run `/bin/payments recover-3ds <id>` or refuse. Wrong because the gate field isn't read deterministically.

### 2E. REFUSAL_FAIL (+6.00) — HIGH risk, INVESTIGATE FIRST
- **Symptom split:** abstained-should-answer (`t010` checkout, `t026` product-existence yes/no over-clarified) AND **security denials that scored 0** (`t013` unauth checkout, `t052`/`t072` cross-customer, `t099` discount fraud).
- **Open question (must resolve before any change):** why do *correct-looking* denials score 0? Two hypotheses: (a) the grader requires a **ref-bearing refusal** (cite the evidence that proves the violation) and a bare deny scores 0 — the `refusal_cite_enforcer.py` should add those refs but may be dead on prod paths; (b) the per-world instantiation made these legitimately answerable and the deny was an over-refusal. **Investigate by reading the keeper traces** (`/home/claude-developer/bitgn-ecom-rel-v0.1.152/logs/20260530_112528/t013__run0.jsonl` etc.) — was a denial reason cited? Were refs attached? Cross-check `refusal_cite_enforcer.py` against prod namespaces.
- **Risk:** HIGH — do NOT loosen any refusal gate without resolving (a) vs (b). If (a): make refusal-cite work on prod paths (additive refs — safe). If (b): extremely careful, family-level only.

### 2F. archive / risk-ops (+0.48) — partial, fraud component
- Partial ~0.76 on "Risk Ops reviewing two-year-old archive export" (the t48-style `/archive` TSV fraud family — memory `project_ecom_fraud_structure`, `project_ecom_t48_genuine_wall`). Likely missing some component members in the fraud cluster (recall). Reuse `fraud_component_completer.py` / `fraud_cluster_filter.py`. Lowest priority (smallest headroom, known hard).

### How to A/B (every family)
```bash
# 1. build/locate a snapshot for the family (artifacts/ws_snapshots, *_real2 oracles)
# 2. baseline vs lever, 5x each:
scripts/local_bench.py --snapshot <dir> --runs 5            # flag OFF
BITGN_USE_<LEVER>=1 scripts/local_bench.py --snapshot <dir> --runs 5   # flag ON
# 3. ship gated-ON for prod ONLY if ON ≥ OFF on the family AND no regression elsewhere.
```
Remember `BITGN_LOCAL_PROD_PATHS=1` and `BITGN_LOCAL_SQL_UNAVAILABLE=1` to emulate prod faithfully.

---

## §7. Open Questions / Investigations

1. **Why do cross-customer / fraud denials score 0** despite being "correct" refusals? (ref-bearing refusal vs over-refusal — §2E.) **Resolve before touching refusals.**
2. **Deterministic counting without SQL or jq-compute** — what is the prod inventory enumeration surface? (`/proc/locations/<city>/<store>.json`? a per-store inventory file?) Probe read-only before designing 2B.
3. **Confirm prod table names** backing `/proc/staff`, `/proc/locations`, `/proc/return-workflows` for the Task 0.1 synth branches.
4. **Per-run variance magnitude** — run v0.1.152 twice more to bound the 73/100 sample (reads are cheap; runs cost slots). Establishes whether a lever's A/B delta exceeds noise.

---

## §8. Reproduction & Artifacts

```bash
# Re-run the contest agent on prod (prod-like parallelism), fetch scores:
cd /home/claude-developer/bitgn-ecom-rel-v0.1.152   # or fresh worktree off v0.1.152
set -a; . ./.env; set +a
export AGENT_MODEL=gpt-5.4 AGENT_REASONING_EFFORT=medium
export BITGN_RUN_NAME="@ai_engineer_helper ECOM1-PROD v0.1.152 release gpt-5.4"
uv run bitgn-agent run-benchmark --benchmark bitgn/ecom1-prod --runs 1 \
  --max-parallel 16 --max-inflight-llm 32 --output artifacts/bench/<name>.json
uv run python scripts/fetch_run_scores.py <run_id> --benchmark bitgn/ecom1-prod --json <scores>.json
```

- Scored run: `artifacts/bench/SCORES_run-22RyWE_v0.1.152_release.json` (mean 0.7863, 73/100)
- Outcome×score xref: `artifacts/bench/XREF_v0.1.152_outcome_score.txt`
- jq probe evidence: `artifacts/prod_explore/jq_probe{,2,3,4}_*.json`
- Original contest run `run-22Ry8RuZK1bonHd8eWqdNNKqc` was never evaluated (state=2, scores unavailable) — that is why the fresh v0.1.152 run was needed.

---

## §9. Self-Review (done)

- **Spec coverage:** every §2 family maps to a §6 spec; foundational A/B-faithfulness gap → Task 0.1; jq wiring → §5; already-done work → §0/Task 0.2.
- **Placeholder scan:** Phase 0 + §5 are fully concrete (exact code/commands). §6 families are intentionally **specs-with-starting-points** (each gets its own TDD sub-plan after code investigation) — flagged as such, not hidden TODOs, because writing fabricated per-family code would violate the no-placeholders rule given the current code wasn't read.
- **Consistency:** flag names (`BITGN_USE_JQ`, `BITGN_LOCAL_PROD_PATHS`, `BITGN_USE_DISPATCH_PLANNER`, `BITGN_USE_REDERIVE_COUNT`) match the codebase pattern; jq grammar matches `project_ecom_prod_jq_contract`.
