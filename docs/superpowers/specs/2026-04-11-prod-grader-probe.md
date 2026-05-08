# PROD Grader Probe — Resolution of Open Question 1

**Date:** 2026-04-11
**Artifact:** `scripts/verify_prod_grader.py`
**Spec reference:** `2026-04-11-routing-skills-and-tools-design.md` §12 Open Question 1.

## Question

Does PROD expose live/mid-run grader feedback (as DEV does), and what fields does the server-side grader surface that our local bench writer does not?

## Method

Inspected the `bitgn.harness_pb2` proto descriptors to enumerate every RPC and message shape on `HarnessService`, then ran a one-task probe against `bitgn/pac1-prod` via the playground flow:

1. `StartPlaygroundRequest(benchmark_id='bitgn/pac1-prod', task_id='t001')`
2. `EndTrialRequest(trial_id=...)` immediately, with no agent work
3. `GetTrialRequest(trial_id=...)` 500 ms later

## Findings

### 1. PROD playground works — and gives LIVE grading

PROD accepts `StartPlaygroundRequest`. The returned `EndTrialResponse` contains a real grader score **immediately** (no embargo):

```json
{
  "state": "TRIAL_STATE_DONE",
  "score": 0.0,
  "score_detail": ["no answer provided"]
}
```

The embargo we observed on previous leaderboard runs (all tasks showed `passes=0` at write time) is a property of `RUN_KIND_BLIND` submissions, **not a property of PROD itself**. The benchmark-level `EvalPolicy` for `bitgn/pac1-prod` is `EVAL_POLICY_OPEN`, which permits live scoring. Blind leaderboard runs opt into the embargo explicitly.

**Implication:** we have a fast iteration loop for PROD. Playground flow ≈ DEV ergonomics, with real grader feedback per trial end, and no leaderboard slot consumed.

### 2. No streaming grader / step-level critique exists

`HarnessService` has exactly these RPCs: `get_benchmark`, `get_run`, `get_trial`, `start_playground`, `start_trial`, `start_run`, `end_trial`, `submit_run`, `status`. None of them stream grader events. `GetTrial` supports incremental log pagination (`cursor` / `next_cursor`), but the `logs` field carries **runtime harness events**, not grader events — confirmed empty (`log_count: 0`) on a no-agent playground trial that had still been graded.

Grader feedback is trial-end-only:
- `EndTrialResponse.score` (float 0..1)
- `EndTrialResponse.score_detail` (list of strings)

Both fields are also echoed on `GetTrialResponse` post-hoc.

### 3. Task previews differ from instructions

`GetBenchmarkResponse.tasks[i]` exposes `task_id`, `preview`, and `hint`. All 104 PROD tasks have a preview; zero have a hint. The preview uses a **stable example entity name** while each playground/run instantiation **substitutes a rotating real entity** into the actual `instruction`.

Example — t001:
- `preview`: "Give me the start date for the project the school routine kit..."
- One playground `instruction`: "Give me the start date for the project the day-job exception project..."

**Implication 1 (routing):** previews are a stable offline corpus for router tuning. We can build and test the regex/classifier pipeline against all 104 previews before running a single trial.

**Implication 2 (generalization):** because entity names rotate per instantiation, any hardcoded answer or entity-specific heuristic will fail on other instantiations. The benchmark is a generalization test by construction — aligns with the "no hardcodes" rule in spec §6.

### 4. Complete GetTrialResponse shape

```
trial_id, instruction, benchmark_id, task_id, error, score,
score_detail, state, logs, next_cursor, run_id
```

`LogLine` fields: `time` (RFC3339 string), `unix_ms` (int64), `text` (string), `kind` (LogKind enum: `SYSTEM`/`REQUEST`/`RESPONSE`/`ERROR`/`CHANGE`/`TELEMETRY`/`USER`), `type` (free-form string), `data` (google.protobuf.Struct — structured JSON blob).

`logs` is a harness runtime event stream, not a grader feedback stream. We already ingest everything else via `scripts/ingest_bitgn_scores.py`. No additional ingest fields are needed for post-hoc score extraction.

## Decisions

1. **Adopt playground flow as the development loop.** Single-task and subset runs against pac1-prod via `StartPlayground` + `EndTrial` give real grader feedback in DEV-like time. Use this for router/skill iteration.
2. **Leaderboard flow (`RUN_KIND_BLIND`) stays reserved for milestone runs.** One per milestone (M0, M2, M4, M6) at most — they're visible on the public dashboard and we want to save those slots for verified releases.
3. **Skip writing a web-scraper for richer grader fields.** None exist: the Connect-RPC surface is the full grader API.
4. **Offline router replay uses `GetBenchmarkResponse.tasks[].preview`.** That's our canonical task corpus for tuning, complemented by the ingested bench JSONs (which carry the instantiated `bitgn_instruction` per trial plus `bitgn_score_detail` failure strings).
5. **Close Open Question 1 in the spec.** Mark it resolved with a pointer to this memo.

## Follow-ups

- The harness `logs` stream is worth exploring for post-run debugging (what did the agent actually do?), but that's out of scope for M0. Flag as a post-M6 optimization.
- The `task.hint` field is currently unused by PROD. If the contest org populates it later, the router would want to consume it — the router API should accept `hint` as an optional input.
