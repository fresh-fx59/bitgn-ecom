# gpt-oss CoT preservation for local tool-calling

Date: 2026-04-16
Status: approved, ready for implementation

## Problem

The `openai/gpt-oss-20b` local benchmark run on 2026-04-15 passed 22/104 PROD
tasks. Log analysis found 10-14 tasks that failed in an
"envelope-parroting" loop: on turn 2+ the model emits an empty
`tool_calls`, drops a default-templated NextStep envelope into `content`
(typically `outcome_leaning=GATHERING_INFORMATION` or
`OUTCOME_NONE_UNSUPPORTED`), and the harness hits `salvage_miss`, logs a
critique, and the model repeats the same envelope on the next turn until
the step budget runs out.

Web research (`https://alde.dev/blog/proper-tool-calling-with-gpt-oss/`)
identified the likely root cause:

> gpt-oss requires the chain-of-thought (CoT), aka reasoning, from past
> tool calls. Preserve on subsequent sampling until a final message gets
> issued. [...] Without passing along the CoT, performance starts to
> degrade the more turns it takes to complete the task. When passing the
> CoT, it completes the task pretty reliably.

LM Studio exposes the per-turn CoT as `choices[0].message.reasoning`
(confirmed in LM Studio 0.3.23+ release notes — the field was moved out
of `message.content` into `message.reasoning` to align with the
o3-mini pattern).

Our `OpenAIToolCallingBackend.next_step` reads `choice.message.content`
and `choice.message.tool_calls` but never captures `message.reasoning`.
On replay, the agent loop emits
`Message(role="assistant", content=step_obj.model_dump_json())` — a flat
JSON dump of the NextStep with no reasoning trace. The model therefore
re-derives context from the system prompt + user task every turn and
decays into the default envelope template.

## Goal

Capture `message.reasoning` on every turn and replay it on the subsequent
request in the canonical OpenAI assistant-message-with-tool_calls shape,
so LM Studio's prompt renderer injects the prior CoT back into the
gpt-oss chat template.

Non-goal: migrate to LM Studio's Responses API endpoint. That may become
necessary if chat-completions replay turns out not to render the CoT; it
is scheduled as a conditional follow-up, not part of this design.

## Scope

Local `openai/gpt-oss-20b` path only — `OpenAIToolCallingBackend`
(`AGENT_TOOLCALLING=1`). `OpenAIChatBackend` (frontier via cliproxyapi)
receives only one mechanical change: its payload builder gains a
`role == "tool"` → `role == "user"` translation (with the existing
`Tool result:\n<content>` prefix) so the agent loop can emit a uniform
provider-agnostic tool-result `Message`. No behavioural change on the
wire — the T24 cliproxyapi/Codex constraint (`agent.py:480-488`) moves
from the agent loop into the backend. Frontier models handle CoT
internally; the backend ignores `reasoning` / `tool_calls` on the
incoming `Message`.

## Architecture

### `backend/base.py`

`Message` gains three optional fields; existing call sites remain valid
because every new field defaults to `None`:

```python
@dataclass(frozen=True, slots=True)
class Message:
    role: str                                 # system | user | assistant | tool
    content: str | None = None                # nullable for assistant+tool_calls turns
    reasoning: str | None = None              # gpt-oss CoT to replay on next turn
    tool_calls: list[dict] | None = None      # raw tool_calls from prior assistant turn
    tool_call_id: str | None = None           # pairs tool-result to its originating call
```

`NextStepResult` gains two optional fields for the agent loop to thread
back:

```python
@dataclass(frozen=True, slots=True)
class NextStepResult:
    parsed: NextStep
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    reasoning: str | None = None
    tool_calls: list[dict] | None = None
```

### `backend/openai_toolcalling.py`

`next_step` captures the reasoning text and the raw `tool_calls` list
from the OpenAI SDK response object:

- `reasoning = getattr(choice.message, "reasoning", None)` — None when
  LM Studio returns a non-reasoning response or the field is absent.
- `tool_calls_raw = [tc.model_dump() for tc in (choice.message.tool_calls or [])]`
  — list of dicts with `id`, `type`, `function.name`, `function.arguments`.
  Empty list when salvage path fires.

Both are stored on the returned `NextStepResult`.

Payload construction moves out of `next_step`'s inline comprehension
into a small helper `_build_payload(messages)` that branches by message
shape:

- `role == "assistant"` and `tool_calls` present → emit
  `{"role": "assistant", "content": None, "tool_calls": [...], "reasoning": <cot>}`.
  `reasoning` key omitted when `None`. Canonical OpenAI / LM Studio
  assistant-with-tool_calls shape.
- `role == "tool"` → emit
  `{"role": "tool", "tool_call_id": <id>, "content": <result>}`.
- else → current behaviour: `{"role": role, "content": content or ""}`.

The fallback branch keeps the salvage path working: when salvage
synthesized a NextStep from content-only text, we lack a real
`tool_call_id`, so the agent loop records the assistant turn as
`content=<json>` with no `tool_calls`, and the payload builder drops
into the else branch.

### `backend/openai_compat.py` (cliproxyapi / frontier)

Untouched. Its payload builder reads `role` + `content` only and
ignores the three new `Message` fields. To keep the tool-result shape
that cliproxyapi's Codex translator requires
(`Message(role="user", content="Tool result:\n...")`), the chat
backend's payload builder is updated to translate `role == "tool"` →
`role == "user"` with the same `Tool result:\n<content>` prefix the
agent loop used to apply inline. The T24 compatibility constraint
moves from the agent loop into the backend where it belongs.

### `agent.py`

The successful-dispatch branch around `agent.py:489-505` currently
writes:

```python
messages.append(Message(role="assistant", content=step_obj.model_dump_json()))
messages.append(Message(role="user", content=f"Tool result:\n{tool_body}"))
```

Becomes:

```python
if result.tool_calls:
    messages.append(
        Message(
            role="assistant",
            content=None,
            tool_calls=result.tool_calls,
            reasoning=result.reasoning,
        )
    )
    tool_call_id = result.tool_calls[0].get("id")
    messages.append(
        Message(role="tool", content=tool_body, tool_call_id=tool_call_id)
    )
else:
    # Salvage path: no native tool_call_id.
    messages.append(Message(role="assistant", content=step_obj.model_dump_json()))
    messages.append(Message(role="user", content=f"Tool result:\n{tool_body}"))
```

`result` is the `NextStepResult` returned from `backend.next_step`.
`result.tool_calls` and `result.reasoning` are populated only by the
toolcalling backend; the chat backend returns them as `None` and the
else branch preserves current behaviour.

Tool-result emission stays provider-agnostic: the agent always records a
`role="tool"` message (with `tool_call_id` when available) and lets each
backend translate to its wire shape.

## Error handling

- `choice.message.reasoning` absent → stored as `None`, omitted from
  payload on replay. No error.
- `tool_calls` absent (salvage path) → assistant turn falls back to
  `content=<json-dump>` shape; subsequent tool result uses the legacy
  `role="user"` path via `tool_call_id=None`.
- Mixed shapes across turns (one turn native, next turn salvaged) →
  each turn's `Message` records its own shape; the payload builder
  handles each independently.
- Chat backend receives `Message` with `reasoning`/`tool_calls` set
  (shouldn't happen — it calls `OpenAIChatBackend.next_step` which
  never populates those fields) → payload builder ignores them.

## Testing

### Unit (new tests in `tests/test_backend_openai_toolcalling.py`)

- `test_payload_builder_emits_canonical_assistant_tool_call_shape`:
  `Message(role="assistant", content=None, tool_calls=[...], reasoning="cot")`
  → payload dict has `content: None`, `tool_calls: [...]`, `reasoning: "cot"`.
- `test_payload_builder_omits_reasoning_when_none`: `reasoning=None` →
  `reasoning` key absent from payload.
- `test_payload_builder_emits_tool_role_with_call_id`:
  `Message(role="tool", content="result", tool_call_id="abc")` → payload
  `{"role":"tool","tool_call_id":"abc","content":"result"}`.
- `test_payload_builder_falls_back_on_salvage_path`: `Message(role="assistant",
  content="<json>")` (no tool_calls) → payload
  `{"role":"assistant","content":"<json>"}`.
- `test_next_step_captures_reasoning_and_tool_calls_into_result`: mock
  completion with `message.reasoning="cot"` and one tool_call → returned
  `NextStepResult.reasoning == "cot"` and `.tool_calls` is the
  structured list.
- `test_next_step_returns_none_reasoning_when_absent`: completion
  without `reasoning` attribute → `NextStepResult.reasoning is None`.

### Unit (new test in `tests/test_backend_openai_compat.py`)

- `test_chat_backend_translates_tool_role_to_user`:
  `Message(role="tool", content="result")` → payload contains
  `{"role":"user","content":"Tool result:\nresult"}`.

### Regression

- Existing 492-test suite must still pass.
- Existing 7 salvage tests still pass (salvage path flows through the
  else branch and emits legacy-shape messages).

### Smoke

- Run **one** PROD trial that historically triggered the envelope loop
  (pick any of the ~10 BACKEND_ERROR tasks from
  `bench_summary_prod_20260415-233621.json`).
- User inspects LM Studio's server log output to confirm:
  - The inbound request on turn 2+ contains a `reasoning` field on the
    prior assistant message, AND
  - The rendered prompt template (visible in LM Studio's verbose/debug
    log) includes the CoT text.
- If rendered prompt contains CoT → proceed with full benchmark.
- If `reasoning` reaches the request body but is dropped before
  templating → pivot to LM Studio Responses API (follow-up design).

## Rollout

1. Implement design on branch `local-toolcalling-lfm2`.
2. Run full test suite locally; confirm no regressions.
3. Run smoke test (1 PROD trial) with user inspecting LM Studio logs.
4. If smoke passes: run full 104-task PROD benchmark. Compare pass rate
   against 22/104 baseline.
5. If smoke fails: open follow-up spec for Responses API migration.

## Follow-ups out of scope for this design

These are acknowledged and will be bundled separately (items 1-4 from
the session conversation):

- Item 1: bump `salvage_miss` log preview 200 → 800 chars for better
  post-mortem diagnosis.
- Item 2: circuit breaker — after N consecutive `salvage_miss` on the
  same turn, synthesize `report_completion(OUTCOME_NONE_UNSUPPORTED)`.
- Item 3: add `<|constrain|>function: {tool: ...}` harmony shape to the
  stripper.
- Item 4: raise `TASK_TIMEOUT_SEC` (1800 → 2400) and
  `LLM_HTTP_TIMEOUT_SEC` (480 → 600) in `.env`; keep
  `AGENT_REASONING_EFFORT=high`.

## References

- <https://alde.dev/blog/proper-tool-calling-with-gpt-oss/> — CoT
  preservation requirement for gpt-oss.
- <https://lmstudio.ai/blog/lmstudio-v0.3.23> — LM Studio release
  announcing `message.reasoning` for gpt-oss (chat completions).
- <https://lmstudio.ai/blog/lmstudio-v0.3.29> — LM Studio Responses API
  support (used as the pivot target if chat-completions replay doesn't
  render CoT).
- <https://platform.openai.com/docs/api-reference/chat/create> —
  canonical assistant+tool_calls message shape
  (`content: null` when `tool_calls` present).
- `bench_summary_prod_20260415-233621.json` — 22/104 baseline.
- `docs/superpowers/specs/2026-04-15-toolcalling-local-model-design.md`
  — prior design that introduced `OpenAIToolCallingBackend`.
