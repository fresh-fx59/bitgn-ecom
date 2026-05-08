# Plan B — Native OpenAI Tool-Calling for Local Models

Date: 2026-04-15
Author: local-toolcalling-lfm2 branch

## Problem

Local LM Studio models (tested: `liquid/lfm2-24b-a2b`, `openai/gpt-oss-20b`)
cannot drive the agent today. Two distinct symptoms, one shared cause:

- **liquid/lfm2-24b-a2b** returns bare OpenAI tool-call shape
  `{"name": "delete", "arguments": {...}}` instead of the expected `NextStep`
  envelope. Every required envelope field is missing. Double validation
  failure → `BACKEND_ERROR`.
- **openai/gpt-oss-20b** returns the envelope shape but with `current_state=""`
  and `observation=""` — both `NonEmptyStr`. 11/11 outcomes in the 211739
  benchmark trial ended in `BACKEND_ERROR`.

Root cause: `OpenAIChatBackend` runs with `use_structured_output=False` (a
cliproxyapi workaround for `oneOf` schema rejection, `openai_compat.py:120`)
and relies on the system prompt to coax the model into emitting the full
`NextStep` JSON object. Frontier models comply; small local models default to
their training-time tool-calling shape.

## Approach

Opt-in, env-flag gated tool-calling backend. Set `AGENT_TOOLCALLING=1` to
activate. Default (off) path is bit-identical to today, so the
79-OK `gpt-5.4` baseline on cliproxyapi is untouched.

### Schema → tools mapping

Each `Req_*` variant (10 tools) plus `ReportTaskCompletion` becomes a separate
OpenAI tool. The envelope fields (`current_state`,
`plan_remaining_steps_brief`, `identity_verified`, `observation`,
`outcome_leaning`) are inlined as **required** parameters on every tool.

Why inline per-tool rather than `function` as a nested discriminated union:
the existing `use_structured_output=False` exists precisely because `oneOf`
was rejected by upstream. Per-tool flat schemas sidestep that entirely —
each tool's parameter schema is a single flat object.

### Backend

New file `src/bitgn_contest_agent/backend/openai_toolcalling.py` implementing
`Backend.next_step`. On each call:

1. POST `chat.completions.create(..., tools=[...], tool_choice="required")`
   with `stream=False`. (`tool_choice="required"` forces a tool call.)
2. Read first `message.tool_calls[0]`. Decode `function.arguments` (JSON).
3. Split envelope fields from tool-specific fields. Construct the matching
   `Req_*` or `ReportTaskCompletion` from the tool-specific fields. Build a
   `NextStep(current_state=..., ..., function=<chosen_req>)`.
4. Return `NextStepResult`.

Any `ValidationError` during step 3 propagates → caller's P3 critique-retry.
Any OpenAI transient error (`RateLimitError`, `APITimeoutError`, etc.) is
wrapped as `TransientBackendError` identically to the compat backend.

### Factory

`cli.py::_make_backend` branches on `AGENT_TOOLCALLING` env. No change to
`AgentConfig` — this is a pure wiring flag that does not affect any other
code path.

### Prompt

`system_prompt()` gets a short conditional paragraph (activated only when
`AGENT_TOOLCALLING=1`) telling the model:

> Call exactly one tool per turn. The envelope fields
> (`current_state`, `plan_remaining_steps_brief`, `identity_verified`,
> `observation`, `outcome_leaning`) are required parameters on every tool —
> fill them before you choose the tool's action parameters.

The existing "emit JSON object" guidance stays — it's still the correct
description when tool-calling is off, and harmless when it's on because the
model is forced by `tool_choice="required"` anyway.

### Timeouts / concurrency

For local inference, `.env` sets:

```
LLM_HTTP_TIMEOUT_SEC=180       # LM Studio 20B model on CPU/MPS is slow
TASK_TIMEOUT_SEC=900           # 15 min per task
MAX_PARALLEL_TASKS=2           # single GPU can't serve 8 concurrent
MAX_INFLIGHT_LLM=2
```

These are local-operator defaults in `.env` only; `config.py` defaults are
unchanged for other users.

## What stays the same

- `AgentLoop` — no code changes. `NextStep` is still the object it operates on.
- `StepValidator` — consumes `observation` / `outcome_leaning` exactly as today.
- `TraceWriter` / `bench_summary` — every step still dumps a full `NextStep`.
- Adapter, session, router, reactive router, format validator: untouched.

## Testing

1. `tests/test_backend_openai_toolcalling.py` — mock-backed unit tests:
   - Tool catalog shape (11 tools, envelope inlined, correct param types).
   - `tool_call → NextStep` happy path for a `Req_Read`.
   - `ReportTaskCompletion` tool call maps correctly.
   - Empty-string envelope field surfaces as `ValidationError`.
   - `RateLimitError` surfaces as `TransientBackendError`.
2. Existing `pytest` suite must stay green (frontier path unchanged).
3. Local smoke: `run-task --task-id t01` end-to-end on `gpt-oss-20b`.
4. Full PROD: `run-benchmark --runs 1` on `gpt-oss-20b`, leaderboard-visible.

## Risks

- Local model may ignore `tool_choice="required"`. Fallback: `tool_choice="auto"`
  and treat a content-only reply as a validation error so P3 retries. Ship as
  a future iteration if the first pass uncovers it.
- Running a 20B model against 104 tasks × up to 40 steps is multi-hour. The
  per-task deadline bounds worst case; the benchmark accepts partial runs and
  the leaderboard submit is idempotent.

## Out of scope

- Restructuring the system prompt.
- Replacing `StepValidator` with a local-model-specific validator.
- Persisting prompt cache across tasks (LM Studio already caches).
- Any change to the frontier path.
