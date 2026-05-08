# BitGN Local Harness Clone — Design

**Date:** 2026-04-26
**Status:** Draft, awaiting user review
**Related prior art:**
- `docs/superpowers/specs/2026-04-11-prod-grader-probe.md` — confirms playground flow + entity rotation
- `scripts/verify_prod_grader.py` — single-task probe prior art
- `scripts/local_bench.py`, `scripts/local_pcm.py` — current filesystem-mock approach (will be deprecated by this work)

---

## Goal

Build a faithful, offline replica of BitGN's PROD harness so that PAC1 fixes can be developed and validated locally without round-tripping through `https://api.bitgn.com`. The replica must:

1. Speak the same gRPC wire protocol as PROD (`harness.proto` + `pcm.proto`).
2. Serve the same per-task workspace state, instruction text, and context date that PROD serves.
3. Score agent submissions using rules reverse-engineered from the PROD grader.
4. Be a drop-in replacement: existing agent code points to a different base URL via env var, nothing else changes.

The system is bootstrapped by a separate scraper program that talks to the real PROD harness, captures everything observable per task, and writes it into a local store. The agent itself is untouched except for one new env-gated trace event.

## Background

PROD exposes `StartPlayground(benchmark_id, task_id) → trial_id, instruction, harness_url`. Playground trials do not consume leaderboard slots and return a real grader score on `EndTrial`. The user has confirmed there is no broad quota — we may make as many API calls as needed.

**Entity rotation (critical).** Per `2026-04-11-prod-grader-probe.md`: each `StartPlayground` invocation for the same `task_id` substitutes a different entity name into the instruction. Workspace contents, expected answers, and grader rules likely rotate alongside. A single scrape cannot capture the task's full state space — we must scrape multiple instantiations per task and treat each as its own row.

**Existing JSONL trace tooling must keep working.** `scripts/intent_report.py`, `scripts/fetch_intent_report.py`, `scripts/aggregate_findCS_findCI.py`, and any other consumer of the bench JSONL format must remain functional. New trace events are additive only.

## Constraints (from user)

1. Scraper is a standalone program; agent code is not modified by it.
2. Agent gets one new env-flagged trace event (LLM prompt+completion content). Default OFF. Existing event kinds untouched.
3. No quota concerns — full PROD scraping is acceptable.
4. Trial sandbox lifecycle and per-task workspace determinism must be validated empirically before scaling.
5. Storage format is design-team's call (SQLite, flat files, or hybrid).
6. Probe matrix order is design-team's call.
7. Existing JSONL tools must keep working.

## Architecture

```
                   ┌─────────────────────────────────┐
                   │     PROD: api.bitgn.com         │
                   │  (HarnessService + PCM gRPC)    │
                   └───────────────┬─────────────────┘
                                   │ playground trials
                                   ▼
                   ┌─────────────────────────────────┐
                   │  scripts/bitgn_scraper.py       │ (NEW, standalone)
                   │   ├─ phase0_lifecycle_spike     │
                   │   ├─ phase1_workspaces          │
                   │   ├─ phase2_probe_grader        │
                   │   └─ phase3_self_validate       │
                   └───────────────┬─────────────────┘
                                   │ writes
                                   ▼
                   ┌─────────────────────────────────┐
                   │  artifacts/harness_db/          │ (NEW)
                   │   ├─ bitgn_local.db (SQLite)    │
                   │   └─ workspaces/<tid>/<inst>/.. │  (flat tree)
                   └───────────────┬─────────────────┘
                                   │ reads
                                   ▼
                   ┌─────────────────────────────────┐
                   │  src/bitgn_local_harness/       │ (NEW)
                   │  gRPC server on localhost:50051 │
                   │  implements harness.proto +     │
                   │  pcm.proto                      │
                   └───────────────┬─────────────────┘
                                   │ same wire protocol
                                   ▼
                   ┌─────────────────────────────────┐
                   │  Existing agent (UNTOUCHED)     │
                   │  + 1 env flag:                  │
                   │    BITGN_TRACE_LLM_CONTENT=1    │
                   │  + 1 env flag (already exists): │
                   │    BITGN_BASE_URL=localhost:..  │
                   └─────────────────────────────────┘
```

The agent already supports `BITGN_BASE_URL` (used by `verify_prod_grader.py`). Pointing it at the local harness is the only change needed at the agent boundary.

## Storage Layer

**Hybrid: SQLite for relational/indexed data, flat files for workspace blobs.**

Rationale: SQLite is one file (clean), supports indexed queries (essential for grader rule lookup), and handles the ~10 KB-scale relational data well. Workspace files are bulkier (often markdown, sometimes 100 KB+), need to be human-greppable, and benefit from filesystem-level diffing during validation. Putting them as BLOBs in SQLite would frustrate inspection.

### SQLite schema

```sql
-- One row per (task_id, instantiation). Each StartPlayground call
-- produces a fresh instantiation_hash. The hash combines instruction
-- text AND workspace tree fingerprint (sha256 of sorted "path:size:sha256"
-- lines for every file) so two trials with identical instructions but
-- different file contents are treated as distinct instantiations.
CREATE TABLE task_instantiations (
    task_id              TEXT NOT NULL,
    instantiation_hash   TEXT NOT NULL,         -- sha256(instruction || tree_fingerprint)
    instruction          TEXT NOT NULL,
    instruction_hash     TEXT NOT NULL,         -- sha256(instruction) — for deduping by instruction alone
    tree_fingerprint     TEXT NOT NULL,         -- sha256 of the workspace tree manifest
    context_time         TEXT NOT NULL,         -- RFC3339 from PCM.Context
    context_unix         INTEGER NOT NULL,
    benchmark_id         TEXT NOT NULL,
    scraped_at           TEXT NOT NULL,
    workspace_dir        TEXT NOT NULL,         -- relative path under workspaces/
    workspace_byte_total INTEGER NOT NULL,
    workspace_file_count INTEGER NOT NULL,
    PRIMARY KEY (task_id, instantiation_hash)
);

CREATE INDEX idx_task ON task_instantiations(task_id);

-- One row per file in the scraped workspace. Mirrors the flat tree;
-- exists for fast aggregate queries (e.g. "what files are common
-- across all instantiations of t000?").
CREATE TABLE workspace_files (
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    path               TEXT NOT NULL,           -- e.g. "10_entities/cast/nina.md"
    is_dir             INTEGER NOT NULL,
    byte_size          INTEGER NOT NULL,
    sha256             TEXT NOT NULL,
    PRIMARY KEY (task_id, instantiation_hash, path),
    FOREIGN KEY (task_id, instantiation_hash)
        REFERENCES task_instantiations(task_id, instantiation_hash)
);

-- Append-only log of every grader probe call.
CREATE TABLE probe_log (
    probe_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    probe_kind         TEXT NOT NULL,           -- 'P1_empty', 'P2_wrong', 'P3_correct_no_refs', etc.
    submitted_answer   TEXT,                    -- exact bytes sent
    submitted_refs     TEXT,                    -- JSON array
    submitted_outcome  TEXT,                    -- 'OUTCOME_OK' | 'OUTCOME_NONE_CLARIFICATION' | etc.
    submitted_writes   TEXT,                    -- JSON map of path → content (or path → null for none)
    score              REAL,
    score_detail_raw   TEXT,                    -- JSON array of strings as returned by grader
    trial_id           TEXT,                    -- PROD trial id, for traceability
    probed_at          TEXT NOT NULL
);

CREATE INDEX idx_probe_task ON probe_log(task_id);

-- Parsed grader rules, derived from probe_log via pattern extractors.
-- Kept separate from probe_log so the raw evidence is preserved and
-- the parser can be re-run on new evidence without losing history.
CREATE TABLE scoring_rules (
    rule_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    rule_kind          TEXT NOT NULL,           -- 'expected_answer' | 'required_ref' | 'required_write' | 'expected_outcome' | 'forbidden_*'
    rule_value         TEXT NOT NULL,           -- the extracted value (e.g. '1989-02-16', or path string)
    confidence         TEXT NOT NULL,           -- 'high' (extracted from grader string) | 'medium' (inferred) | 'low' (LLM-judged)
    derived_from       INTEGER NOT NULL,        -- probe_log.probe_id this rule was extracted from
    notes              TEXT,
    FOREIGN KEY (derived_from) REFERENCES probe_log(probe_id)
);

CREATE INDEX idx_rules_task ON scoring_rules(task_id, instantiation_hash);
```

### Flat file layout

```
artifacts/harness_db/
├── bitgn_local.db
├── workspaces/
│   └── <task_id>/
│       └── <instantiation_hash[:12]>/
│           ├── _meta.json               -- {instruction, context_time, scraped_at, ...}
│           ├── 10_entities/
│           │   └── cast/
│           │       └── nina.md
│           ├── 50_finance/
│           │   └── ...
│           └── ...
└── scrape_runs/
    └── <YYYYMMDD_HHMMSS>/
        ├── scraper.log                  -- per-call timing, errors
        ├── lifecycle_spike.json         -- empirical findings from phase 0
        ├── determinism_report.json      -- which tasks rotate, which don't
        └── probe_summary.json           -- coverage stats (rules extracted per task)
```

The hash prefix (first 12 chars) is sufficient for uniqueness within a task and keeps directory names human-readable.

## Component 1: Scraper (`scripts/bitgn_scraper.py`)

Standalone program. Imports `bitgn.harness_pb2` and `bitgn.pcm_pb2` for gRPC types. Does **not** import any agent code.

### Phase 0: Lifecycle spike

Goal: validate empirical assumptions before any scaled work. **Requires `BITGN_API_KEY` in env**, sourced from `.env` like `verify_prod_grader.py`.

Steps:
1. Call `StartPlayground(t001)` 3 times in succession. Compare the 3 instructions. **Records:** is the instruction text identical? Different? How many distinct variants emerge over N=20 calls? (Answers: does rotation exist? Is the variant pool finite?)
2. For one trial: time how long `harness_url` remains reachable after `EndTrial`. Probe at t=0s, t=5s, t=30s, t=5min, t=30min. **Records:** does the sandbox stay alive after EndTrial, or is it torn down immediately? (Determines probe Strategy A vs B.)
3. For one trial without calling `EndTrial`: probe `harness_url` at t=10min, t=30min, t=2h. **Records:** is there a max trial lifetime that auto-terminates?
4. Try writing a file via `PCM.Write` then `EndTrial` → start a new trial of the same `task_id` → does the new trial see the write? **Records:** is workspace state per-trial-isolated, or shared?
5. Submit two `Answer` calls in the same trial (A1=wrong, A2=different-wrong), then `EndTrial`. **Records:** does the grader use the last `Answer` or the first? Critical for Strategy A.
6. **Rate-limit discovery.** Issue 20 parallel `StartPlayground` calls; record HTTP/429 or throttling response. **Records:** rate ceiling; informs scraper concurrency.
7. **Workspace size sanity.** For 5 tasks, sum file sizes from full tree-walk. **Records:** does any task exceed 10 MB, 100 MB? Sets per-task disk safety abort threshold.

Output: `scrape_runs/<ts>/lifecycle_spike.json`. Findings inform scraper concurrency, retry strategy, probe strategy A vs B, and how many instantiations per task to capture.

Estimated wall-clock cost for full pipeline (Phase 1+2 across 104 tasks): ~14 h sequentially at ~10 s/playground call × ~30 calls/task. Phase 0 step 6 may unlock parallelism that brings this to 2–4 h. Disk: assume ≤10 MB/instantiation × 30 instantiations × 104 tasks = ~30 GB worst case; safety abort at 100 MB/instantiation.

### Phase 1: Workspace scrape

For each task_id in the benchmark (104 for `bitgn/pac1-prod`):

```python
def scrape_task(task_id, n_instantiations):
    seen_hashes = set()
    for _ in range(n_instantiations):
        trial = client.start_playground(StartPlaygroundRequest(...))
        inst_hash = sha256(trial.instruction)[:64]
        if inst_hash in seen_hashes:
            continue  # duplicate variant, skip
        seen_hashes.add(inst_hash)

        pcm = PcmClient(trial.harness_url)
        ctx = pcm.context()
        tree = pcm.tree(root="/")
        # walk tree, read every file
        files = walk_and_read_all(pcm, tree)

        write_to_db_and_disk(task_id, inst_hash, trial.instruction, ctx, files)

        # Reserve trial for grader probing in Phase 2
        # (or end_trial here if probing is interleaved)
```

`n_instantiations` is determined empirically from Phase 0. Default starting target: scrape until 5 consecutive calls produce only duplicates (saturation heuristic), capped at 30.

The walker handles arbitrarily deep trees. If a `Read` fails (binary file? encoding error?) the file is recorded with `byte_size=0, sha256='READ_ERROR'` and a note in `scraper.log` — we don't abort the scrape on a single bad file.

### Phase 2: Grader probe matrix

**Rotation correctness.** Each `StartPlayground` produces a fresh instantiation. Naively chaining "submit P1 → use leaked answer in P2" across trials is wrong: P2's trial may target a different entity rotation and the answer extracted from P1 won't apply. Two execution strategies, picked based on Phase 0 finding Q2 (does the sandbox stay alive after `EndTrial`?):

- **Strategy A (preferred, if sandbox persists post-EndTrial or supports multi-Answer-per-trial):** open one trial per instantiation, run P1–P5 sub-probes against the same `harness_url`, aggregate leaks per-instantiation.
- **Strategy B (fallback):** one trial per probe, aggregate by post-hoc `instruction_hash` matching. Each probe trial records its own instantiation; rules are merged across trials sharing identical instruction text.

For each `(task_id, instantiation_hash)` row from Phase 1, run probes in this order:

| Probe | Answer | Refs | Writes | Outcome | Stops if score=1.0 |
|---|---|---|---|---|---|
| P1 | `""` | `[]` | `{}` | `OUTCOME_OK` | yes — task fully understood |
| P2 | extracted-answer | `[]` | `{}` | `OUTCOME_OK` | yes |
| P2b | extracted-answer with semantic-neighbor mutation (date±1, name case-flip, trailing whitespace, alt format) | `[]` | `{}` | `OUTCOME_OK` | n/a — diagnostic; tests answer-tolerance |
| P3 | extracted-answer | extracted-refs | `{}` | `OUTCOME_OK` | yes |
| P4 | extracted-answer | extracted-refs | extracted-writes | `OUTCOME_OK` | yes |
| P5 | extracted-answer | extracted-refs | extracted-writes | `OUTCOME_NONE_CLARIFICATION` | terminal |
| P6 | random-but-typed (date/number/name matching question shape) | `[]` | `{}` | `OUTCOME_OK` | n/a — diagnostic on a sample |

P2b is the multi-valued-answer test: if the grader accepts `1989-02-17` when expecting `1989-02-16`, score=1.0 reveals tolerance; otherwise we know it's exact-match. Run on 20 sampled tasks per category to characterise grader strictness, not all 104.

After each probe, parse `score_detail` with regex extractors:

```python
PATTERNS = [
    (r"answer is incorrect\. Expected: '([^']+)'",         'expected_answer'),
    (r"missing file write '([^']+)'",                      'required_write'),
    (r"answer missing required reference '([^']+)'",       'required_ref'),
    (r"expected outcome (\w+), got (\w+)",                 'expected_outcome'),
    (r"answer must include the (\w+) of",                  'answer_constraint'),
    # extend as new patterns emerge from real data
]
```

Each match → row in `scoring_rules` with `confidence='high'`. Detail strings that don't match any pattern → flagged for LLM-based interpretation in Phase 3.

**Adaptive stopping:** if any probe returns `score=1.0`, that instantiation is "fully understood" — record the rule set, stop probing this row. If P5 (the outcome-mismatch probe) is reached without `score=1.0`, the task likely uses semantic similarity scoring; flag `confidence='low'` and move on.

P6 is purely diagnostic — its result tells us whether the grader does string-equality on answers or semantic matching. Run on ~10 sampled tasks, not all 104.

### Phase 1.5: Free seed rules from existing traces

Before running any probes, mine `logs/prod_cf90740_full/20260425_181902/*.jsonl` and the 7 PROD server logs (`vm-*.eu.bitgn.com.txt`, `t000-*.log`, `t066-*.log`) for already-observed `score_detail` strings. The 22LAfu4 trace has 2 failed tasks (t000, t066) with explicit "Expected:" / "missing file write" strings; the historical server logs contribute another ~5 failed-task patterns. Apply the regex extractors from Phase 2 to these strings; populate `scoring_rules` with `confidence='high'` and `derived_from=NULL` (no probe id). This gives free coverage on a non-trivial slice before any new API calls.

### Phase 3: Self-validation

After phases 1 & 2 complete, run validators:

1. **Determinism diff.** Re-scrape 10 random tasks. For each `task_id`, compare new instantiation hashes against stored. If a "new" instantiation hash actually has identical instruction-text to a stored one (false-positive hash mismatch), there's a bug; if all are duplicates → rotation pool fully covered; if many are new → not yet saturated, expand `n_instantiations`.
2. **Workspace file integrity.** SHA-256 compare flat files vs `workspace_files.sha256`. Detect bit-rot or partial writes.
3. **Probe coverage stats.** `probe_summary.json` reports: how many tasks have at least one rule with `confidence='high'`; how many have `confidence='low'` only (the residual hard set).
4. **LLM-based shape check.** For tasks with no machine-extractable rules, dump (instruction, all probe results) to a JSON file. The user (or me, in a separate Claude Code session) can inspect manually and write fallback rules. Not automated yet — flagged as a follow-up.

## Component 2: Local gRPC harness (`src/bitgn_local_harness/`)

A Python gRPC server, started by `scripts/run_local_harness.py` on `localhost:50051` (configurable). Implements:

- `HarnessService.StartPlayground(benchmark_id, task_id)` — picks one instantiation_hash for the task (round-robin or deterministic-by-seed; configurable), returns synthetic `trial_id` and a `harness_url` pointing to its own PCM endpoint (e.g. `http://localhost:50052/<trial_id>`).
- `HarnessService.EndTrial(trial_id)` — looks up trial's `(task_id, inst_hash)`, looks up agent's submitted answer/refs/writes from in-memory trial state, runs `score_with_local_rules(...)`, returns `(score, score_detail)`.
- `HarnessService.GetTrial`, `Status`, `GetBenchmark`, `GetRun`, `StartTrial`, `StartRun`, `SubmitRun` — implemented as needed for agent compatibility.
- All `PcmRuntimeService` RPCs — backed by an in-memory copy of the workspace files (loaded from disk on trial start, mutations stay in trial-local memory). One trial per `harness_url` path prefix.

`score_with_local_rules`:
```python
def score(submitted_answer, submitted_refs, submitted_writes, submitted_outcome,
          rules: list[ScoringRule]) -> (float, list[str]):
    failures = []
    for rule in rules:
        if rule.rule_kind == 'expected_answer' and submitted_answer != rule.rule_value:
            failures.append(f"answer is incorrect. Expected: '{rule.rule_value}'")
        elif rule.rule_kind == 'required_write' and rule.rule_value not in submitted_writes:
            failures.append(f"missing file write '{rule.rule_value}'")
        elif rule.rule_kind == 'required_ref' and rule.rule_value not in submitted_refs:
            failures.append(f"answer missing required reference '{rule.rule_value}'")
        elif rule.rule_kind == 'expected_outcome' and submitted_outcome != rule.rule_value:
            failures.append(f"expected outcome {rule.rule_value}, got {submitted_outcome}")
    return (1.0 if not failures else 0.0, failures)
```

This mirrors PROD's all-or-nothing scoring (we have not seen partial credit in any observed `score_detail`). For tasks where `confidence='low'` (no rules extracted), the local harness returns `score=0.0, score_detail=["LOCAL_HARNESS: rules unknown for this task"]` so the agent can still run end-to-end without hanging.

The harness supports a `BITGN_LOCAL_HARNESS_VARIANT_SEED` env var so tests can pin to a deterministic instantiation across runs.

## Component 3: Agent LLM-trace gate

**Existing prior art.** Commit 647b1e8 already ships `BITGN_TRACE_RAW_RESPONSES=1` via `src/bitgn_contest_agent/adapter/pcm_tracing.py`, which dumps every PCM request/response pair as JSONL to `logs/raw_responses/`. That covers the *workspace I/O* side. This component adds the symmetric piece for LLM calls — it does **not** duplicate the PCM raw-response work.

**Call site located:** `src/bitgn_contest_agent/backend/openai_compat.py:259` (`call_structured`) and the abstract base at `src/bitgn_contest_agent/backend/base.py:52`. The wrapper hooks the concrete subclass return path so all backends (current + future) inherit the trace.

Today the trace emits `kind=step` with `llm.prompt_tokens`, `llm.completion_tokens`, etc., but no prompt or completion content. New behavior:

- If `BITGN_TRACE_LLM_CONTENT=1` (or `=true` / `=yes`), emit an additional event after each LLM call:
  ```json
  {"kind": "llm_call", "step": <int>, "task_id": "<id>",
   "model": "...", "system": "...", "prompt": "...",
   "completion": "...", "reasoning": "...",
   "prompt_tokens": <int>, "completion_tokens": <int>,
   "latency_ms": <int>}
  ```
- Default OFF. When unset or `=0`, behavior is byte-identical to today.
- Sibling env var `BITGN_TRACE_LLM_DIR` (optional) overrides the dump location, mirroring the `BITGN_TRACE_RAW_DIR` pattern from 647b1e8 for consistency.
- New event has a new `kind` value — existing JSONL consumers ignore it (they all filter by specific `kind` strings).

Existing tools verified to keep working:
- `scripts/intent_report.py` — filters `kind=meta`, `kind=outcome`. Unaffected.
- `scripts/fetch_intent_report.py` — uses gRPC, not JSONL. Unaffected.
- `scripts/aggregate_findCS_findCI.py` — filters `kind=step` for OPT_A markers. Unaffected.
- `scripts/local_bench.py` — writes traces, doesn't read them. Unaffected.
- `BITGN_TRACE_RAW_RESPONSES` capture (from 647b1e8) — independent code path, both flags can be on simultaneously.

## Phasing & deliverables

| Phase | Deliverable | Depends on |
|---|---|---|
| **0** | `scripts/bitgn_scraper.py phase0_spike` + `lifecycle_spike.json` | nothing |
| **1** | `scripts/bitgn_scraper.py phase1_workspaces` + populated `task_instantiations` + `workspace_files` + flat tree | Phase 0 findings |
| **2** | `scripts/bitgn_scraper.py phase2_probe` + populated `probe_log` + `scoring_rules` | Phase 1 |
| **3** | `scripts/bitgn_scraper.py phase3_validate` + `determinism_report.json` + `probe_summary.json` | Phase 2 |
| **4** | `services/bitgn_local_harness/` server + `scripts/run_local_harness.py` | Phase 3 (data complete) |
| **5** | Agent `BITGN_TRACE_LLM_CONTENT` env flag + new `kind=llm_call` event | nothing — independent |
| **6** | Acceptance: existing `scripts/local_bench.py` re-pointed at local harness, runs full 104 task replay, agreement-vs-PROD report | Phases 0–4 |

Phases 0–3 form one implementation plan (the scraper). Phase 4 is a second plan (the server). Phase 5 is a third plan (agent trace), parallel-safe with everything else. Phase 6 is acceptance, not a plan.

## Validation strategy (how do we know it works?)

The existence test for this whole effort: **a task that fails on PROD must also fail on the local harness, with a matching `score_detail` string.** Specifically:

1. **t000 reproduction.** Run the existing agent against the local harness's `t000` instantiation that matches the cf90740 PROD failure (instruction asks about partner-born, agent reads Nina). Local harness must emit `score=0.0, score_detail=["answer is incorrect. Expected: '1989-02-16'"]`.
2. **t066 reproduction.** Same pattern: agent OCRs only juniper_ssd.md, local harness must emit `missing file write '...hearthline_sensor_bundle.md'` and the second missing-write string.
3. **Pass-rate parity.** Run the cf90740 agent against the local harness over 5 runs of all 104 tasks. Compute mean pass count. PROD's cf90740 22LAfu4 baseline is 102/104. Local mean must be within ±5 tasks (i.e. ≥97/104 mean across 5 runs). The 5-task slack accounts for LLM stochasticity that's already known to dominate per-trial outcomes.

A failure on (1) or (2) is a blocker. A failure on (3) larger than 5% is investigated before declaring the harness production-ready.

## Open questions to resolve empirically

These are answered by Phase 0 and don't block design approval — they're flagged so the implementation plan knows what it needs to discover:

- **Q1.** Does `StartPlayground` for the same task_id return identical instructions (no rotation) or distinct ones? How many distinct variants exist per task?
- **Q2.** Is the `harness_url` sandbox alive after `EndTrial`? For how long?
- **Q3.** Do trials auto-terminate after a max wall time without `EndTrial`? At what threshold?
- **Q4.** Is workspace state isolated per trial, or shared across trials of the same task? (I assume isolated based on PROD design; verify.)
- **Q5.** Are there tasks where `score_detail` is empty even on score=0.0? If so, we cannot extract rules for those tasks — they need a fallback (LLM-judged) rule mode.

## Out of scope

- LLM-as-judge fallback grader for tasks with semantic-similarity scoring. Flagged as a follow-up — the framework supports it via `confidence='low'` rules, but we don't implement it in this work. Tasks left in `confidence='low'` after Phase 1.5 + Phase 2 are reported as a residual set; the parity acceptance gate (Validation §3) excludes them with a documented count.
- Replicating PROD's leaderboard / `RUN_KIND_BLIND` flow. The local harness only models the playground/`EVAL_POLICY_OPEN` path.
- Scraper UI / admin dashboard. This is a CLI program with JSON outputs.
- Removing `scripts/local_pcm.py` and `scripts/local_bench.py`. They keep working off filesystem snapshots and are a useful sanity-check fallback during the local-harness rollout. Deprecation is post-Phase 6.
- Forbidden-value rules ("answer must NOT contain X"). Probe matrix only extracts positive constraints. Flag if encountered.

## Plan decomposition

Three plans, executed in order with the third in parallel:

1. **`docs/superpowers/plans/2026-04-26-bitgn-scraper-and-storage.md`** — Phases 0–3 of the scraper (lifecycle spike, workspace scrape, probe matrix, self-validate). Code lives at `scripts/bitgn_scraper.py` plus a small `src/bitgn_scraper/` package for shared helpers.
2. **`docs/superpowers/plans/2026-04-26-bitgn-local-harness-server.md`** — Phase 4 (gRPC server + integration test against scraped DB). Code at `src/bitgn_local_harness/` + `scripts/run_local_harness.py`.
3. **`docs/superpowers/plans/2026-04-26-bitgn-agent-llm-trace-gate.md`** — Phase 5 (env-var-gated `kind=llm_call` event in agent). Touches the agent's existing LLM call site only.

Plan 1 must complete before Plan 2 starts. Plan 3 is independent and may ship at any point.
