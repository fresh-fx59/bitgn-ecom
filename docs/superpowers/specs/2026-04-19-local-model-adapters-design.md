# Per-Model Adapter Architecture for Local LLMs — Design

**Date:** 2026-04-19
**Scope:** `src/bitgn_contest_agent/backend/openai_toolcalling.py` and new `backend/adapters/` tree.
**Applies only when:** `AGENT_TOOLCALLING=1`. The frontier path (`OpenAIChatBackend`) is untouched.

---

## Motivation

A single tool-calling backend was incrementally tuned for `openai/gpt-oss-20b` via a growing `_try_salvage_from_content` helper. On 2026-04-19 a run of `glm-4.7-flash-mlx` scored 0/2 on completed tasks because the gpt-oss-tuned bare-value salvage captured GLM's stray chat-template tokens (`"</tool_call>"`) and packaged them as the task answer. Other observed cross-model frictions:

- GLM-4.7 Flash crashes LM Studio ("The model has crashed...") at >1 concurrent request due to memory pressure. `MAX_PARALLEL_TASKS=3` is an active footgun.
- LFM2 emits the bare OpenAI tool shape `{"name","arguments"}` as free-text content; that shape is meaningless for GLM.
- Timeouts, reasoning_effort, and concurrency caps differ per model, today all driven from a single `.env` block that must be edited before every model switch.

Supporting a new local model currently requires reading the 903-line backend file, identifying which salvage branches apply, editing a monolith, and re-running. We need per-model quirks to be additive, not intertwined.

## Non-goals

- Not changing the frontier path (`OpenAIChatBackend`, `cliproxy`, `gpt-5.3-codex`).
- Not restructuring the agent loop, validator, classifier, or orchestrator.
- Not introducing a wire-level abstraction over OpenAI chat completions. The adapter shapes request payloads and parses response messages; HTTP transport remains the SDK default.

## Decisions & rationale

Six decisions fixed during brainstorming (all enumerated for traceability):

| # | Decision | Summary |
|---|---|---|
| Q1 | **Full wire-level adapter (originally C, trimmed)** | Adapter shapes outbound requests and parses inbound responses. In practice only response parsing diverges today; request shaping is a no-op for all four models. Hook kept for future use. |
| Q2 | **Monolithic `ModelAdapter` subclass per model (A, flipped from C after self-critique)** | Initial design used strategy composition (`RequestShaper` + `ResponseParser` + `ContentSalvage` Protocols). Rejected during self-critique: every adapter has a 1:1 mapping with no strategy sharing. Four protocols + ten strategy classes for zero real composability = YAGNI. Reverted to a single `ModelAdapter` base class with two overridable methods. |
| Q3 | **Exact-string registry + fail-fast** | `ADAPTERS: dict[str, type[ModelAdapter]]`. Unknown model at `AGENT_TOOLCALLING=1` aborts at startup with an error listing registered keys. Consistent with existing fail-fast `ConfigError` pattern. |
| Q4 | **Adapter covers all LLM call sites + profile governs concurrency** | Applies to both `next_step` and `call_structured`. `ModelProfile.max_parallel_tasks` / `max_inflight_llm` feed the orchestrator semaphore so per-model safe concurrency is the default. Env var still wins (precedence table in §4). |
| Q5 | **Adapter scoped to `OpenAIToolCallingBackend` only** | Frontier path (`OpenAIChatBackend`) never consults the registry. Fail-fast for unknown model only fires when `AGENT_TOOLCALLING=1`. Preserves the "frontier path is bit-identical" guarantee. |
| Q6 | **One file per adapter under `backend/adapters/`** | Each model's quirks co-located with its name. Strategy helpers in `adapters/_helpers.py`; registry in `adapters/__init__.py`. |

Self-critique outcomes folded in:

- Parser vs. salvage merged into one `extract_next_step(message) → NextStep | None` method.
- `supports_tool_choice_required` flag dropped (speculative, no model needs it).
- `ModelProfile` kept as a single dataclass but explicitly documented as a deliberate conflation with a splitting trigger (>8 fields or caller needs per-knob override).
- Precedence rule for env/profile/hard-default encoded and logged at startup.
- Salvage guard preservation elevated to its own implementation task.
- Registry key drift handled by documentation, not normalization.

## 1. Architecture overview

```
OpenAIToolCallingBackend
    └── adapter: ModelAdapter                 ← injected at from_config(model=...)
           ├── profile: ModelProfile           (timeouts + concurrency + reasoning_effort)
           ├── shape_request(payload) -> payload
           └── extract_next_step(message) -> NextStep | None
```

`next_step` flow:

```
payload = adapter.shape_request(build_payload(messages))
msg = http.post(...)
result = adapter.extract_next_step(msg)
if result is None:
    raise ValidationError(...)
return result
```

No parser/salvage split. One method, one responsibility: turn a completion message into a `NextStep` or give up.

Same flow for `call_structured`, which today uses `response_format=<schema>`; the adapter does not need to override it unless a future model requires special handling.

## 2. Adapter interface

### 2.1 `ModelProfile`

```python
@dataclass(frozen=True, slots=True)
class ModelProfile:
    """
    NOTE: Deliberately conflates three scopes (orchestrator concurrency, HTTP
    timeouts, per-call model knobs). Fine at this size. Trigger for splitting:
    the dataclass grows past ~8 fields, OR a caller needs to override one knob
    (e.g., classifier wants reasoning_effort="low" while agent wants "high")
    without replacing the whole profile.
    """
    task_timeout_sec: int
    llm_http_timeout_sec: int
    classifier_timeout_sec: int
    max_parallel_tasks: int
    max_inflight_llm: int
    reasoning_effort: str   # "low" | "medium" | "high"
```

### 2.2 `ModelAdapter` base class

```python
class ModelAdapter:
    """Override the two methods you care about. Base provides standard
    OpenAI tool_calls extraction for models that behave correctly."""

    name: str
    profile: ModelProfile

    def __init__(self, name: str, profile: ModelProfile) -> None:
        self.name = name
        self.profile = profile

    def shape_request(self, payload: dict) -> dict:
        """Default: passthrough. Override for system-message injection,
        tool-schema trimming, tool_choice fallback, etc."""
        return payload

    def extract_next_step(
        self, message: ChatCompletionMessage,
    ) -> NextStep | None:
        """Default: standard OpenAI tool_calls[0] path. Returns None on
        failure; backend translates None into ValidationError. Override
        to chain model-specific fallbacks after the standard attempt."""
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            return None
        call = tool_calls[0]
        try:
            args = json.loads(call.function.arguments or "{}")
            return _build_next_step(call.function.name, args)
        except (json.JSONDecodeError, ValidationError):
            return None
```

### 2.3 Concrete adapters

Each concrete adapter lives in `adapters/<name>.py`, is <50 lines, and follows the template:

```python
# adapters/gpt_oss.py
class GptOssAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="openai/gpt-oss-20b",
            profile=ModelProfile(
                task_timeout_sec=2400,
                llm_http_timeout_sec=600,
                classifier_timeout_sec=300,
                max_parallel_tasks=4,
                max_inflight_llm=4,
                reasoning_effort="high",
            ),
        )

    def extract_next_step(self, message):
        # 1. standard tool_calls path
        result = super().extract_next_step(message)
        if result is not None:
            return result
        # 2. content-based fallbacks, in priority order
        content = (message.content or "")
        return (
            _try_harmony_header(content)
            or _try_bare_name_arguments(content)
            or _try_envelope(content)
            or _try_envelope_terminal(content)
            or _try_bare_value(content)   # last resort
        )
```

The salvage helpers (`_try_harmony_header`, `_try_bare_name_arguments`, etc.) live in `adapters/_helpers.py` as plain module-level functions — no classes, no strategy pattern. Each is ~15 lines, accepts `str`, returns `NextStep | None`.

## 3. Per-model behavior matrix

| Model | `extract_next_step` chain | `max_parallel_tasks` | `max_inflight_llm` | `task_timeout_sec` | `llm_http_timeout_sec` | `reasoning_effort` |
|---|---|---|---|---|---|---|
| **openai/gpt-oss-20b** | standard → harmony → bare-name-arguments → envelope → envelope-terminal → bare-value | 4 | 4 | 2400 | 600 | high |
| **glm-4.7-flash-mlx** | standard only | 1 | 1 | 3600 | 900 | medium |
| **liquid/lfm2-24b-a2b** | standard → bare-name-arguments | 2 | 2 | 1800 | 600 | medium |
| **qwen3.5-35b-a3b** | standard only (grow on evidence) | 2 | 2 | 1800 | 600 | medium |

**Critical: GLM gets NO salvage.** Its "content" is chat-template leakage (`"</tool_call>"`, `"<|channel|>"`, etc.), never a real answer. Empty `tool_calls` → raise → agent's P3 critique retry path handles it. This is the root-cause fix for the 2026-04-19 score=0 incident.

## 4. Profile precedence

At startup, the CLI resolves each tunable in this order:

| Priority | Source | Notes |
|---|---|---|
| 1 (highest) | Environment variable | Explicit operator override — always wins |
| 2 | Adapter profile | Per-model safe default |
| 3 (lowest) | `AgentConfig` hard default | Used only when no adapter is active (frontier path) |

Tunables this rule covers: `max_parallel_tasks`, `max_inflight_llm`, `task_timeout_sec`, `llm_http_timeout_sec`, `classifier_timeout_sec`, `reasoning_effort`.

CLI logs one line at resolution time:

```
[ARCH:CONFIG] resolved adapter=GlmFlashAdapter
  max_parallel_tasks=1 (source=adapter)
  max_inflight_llm=1 (source=adapter)
  task_timeout_sec=3600 (source=env)
  llm_http_timeout_sec=900 (source=adapter)
  reasoning_effort=medium (source=adapter)
```

Implementation detail: `AgentConfig` fields for these tunables become `Optional[int] | Optional[str]`. `load_from_env` returns `None` when env var is unset. The resolver runs after config load and adapter selection, replacing the `None`s.

## 5. Registry + selection

`adapters/__init__.py` exports:

```python
ADAPTERS: dict[str, type[ModelAdapter]] = {
    "openai/gpt-oss-20b":   GptOssAdapter,
    "glm-4.7-flash-mlx":    GlmFlashAdapter,
    "liquid/lfm2-24b-a2b":  Lfm2Adapter,
    "qwen3.5-35b-a3b":      QwenA3bAdapter,
}

def get_adapter(model: str) -> ModelAdapter:
    cls = ADAPTERS.get(model)
    if cls is None:
        raise ConfigError(
            f"No adapter registered for model {model!r}. "
            f"Registered: {sorted(ADAPTERS)}. "
            f"Add one in src/bitgn_contest_agent/backend/adapters/."
        )
    return cls()
```

`get_adapter` is called only from `OpenAIToolCallingBackend.from_config`, which itself is only called when `AGENT_TOOLCALLING=1`. Frontier (`OpenAIChatBackend`) path never calls `get_adapter` — model names like `gpt-5.3-codex` don't need registry entries.

**Model-string drift policy:** registry keys are exact strings matching what LM Studio reports in `/v1/models`. If LM Studio renames a model (case, quant suffix, etc.), add a new registry entry pointing at the same adapter class. No normalization — normalization logic is its own source of bugs and encourages "why doesn't my new variant match" debugging.

## 6. Salvage guard preservation

Each of the five existing salvage branches in `_try_salvage_from_content` accreted guards from real incidents. The implementation plan includes one task per branch:

- **Bare-value**: preserves `len(stripped) ≤ 80 and len(stripped.split()) ≤ 5 and "{" not in stripped`. Originating commit: the bare-value salvage commit from 2026-04-16 (gpt-oss-20b PROD run closeout).
- **Empty-string placeholder injection** for `rulebook_notes`, `outcome_justification`, `message`: preserves the `== ""` replacement with `"—"`. Originating commit: envelope-branch tightening.
- **Harmony header tolerance**: preserves the regex accepting both `commentary to=functions.X` and `functions.X`. Originating: `_strip_harmony` introduction.
- **`ValidationError` fall-through**: preserves the `try/except ValidationError: pass` — never surface, always fall through to next fallback. Originating: multiple commits; rationale is that `_build_next_step` rejecting one shape doesn't mean the message is garbage.
- **Envelope-terminal fallback** for outcome_leaning telegraphed without a `function`/`name`: preserves `_maybe_salvage_envelope_terminal`. Originating: envelope-shaped terminal salvage commit.

Each task:
1. Cites the git commit that introduced the guard.
2. Copies the guard into the new adapter's helper unchanged.
3. Asserts the guard is exercised by a named test that reproduces the original failure (copied from `tests/test_backend_openai_toolcalling.py`).

## 7. Migration plan

Six steps, each a committable unit. TDD throughout: tests copied first, implementation follows.

1. **Scaffold** — create `adapters/__init__.py`, `adapters/base.py` (with `ModelProfile` + `ModelAdapter` base class), `adapters/_helpers.py` (empty). No backend change. Tests: `ModelAdapter` base `extract_next_step` standard path.
2. **Salvage helpers extraction** — copy the five salvage branches from `_try_salvage_from_content` into `_helpers.py` as module-level functions with existing guards. Tests: copy-paste from `test_backend_openai_toolcalling.py`, one per branch.
3. **Concrete adapters** — create `gpt_oss.py`, `glm_flash.py`, `lfm2.py`, `qwen_a3b.py`. Each wires its chain. Tests: per-adapter file mirroring structure.
4. **Registry + fail-fast** — populate `ADAPTERS`, add `get_adapter`. Test: unknown model raises `ConfigError` with listed registered keys.
5. **Wire adapter into backend** — `OpenAIToolCallingBackend.from_config` calls `get_adapter(model)`, stores on instance. `next_step` and `call_structured` call through adapter. Delete `_try_salvage_from_content`, `_strip_harmony`, the harmony regexes, and the salvage branches from `openai_toolcalling.py` once adapters own them.
6. **Profile precedence resolution** — add resolver in CLI startup. `AgentConfig` optional-field migration. Log resolved values.

Each step leaves the suite green. Step 5 is the biggest single cut-over; a pre-step-5 commit must confirm that `GptOssAdapter.extract_next_step` returns byte-identical output to today's `next_step` on recorded traces (regression guard).

After step 6: GLM works correctly (no salvage misfire) and concurrency=1 is automatic. No code changes needed for that — the adapter's profile handles it.

## 8. Testing

Test layout mirrors adapter layout:

```
tests/backend/adapters/
    test_base.py               # ModelAdapter base + ModelProfile
    test_helpers.py            # one test per salvage helper + its guards
    test_gpt_oss.py            # extraction chain, profile values
    test_glm_flash.py          # standard-path-only, profile values, "no salvage" regression
    test_lfm2.py               # standard + bare-name-arguments chain
    test_qwen_a3b.py           # standard path, profile values
    test_registry.py           # get_adapter success, unknown-model ConfigError
```

The existing `test_backend_openai_toolcalling.py` shrinks to cover only dispatch (backend calls adapter; resolved profile values; `call_structured` uses adapter). Salvage-specific tests migrate to the per-adapter or per-helper files.

Regression guards (the salvage guard preservation tasks from §6) each get a named test whose docstring cites the originating commit.

## 9. Open questions / known risks

- **Qwen3.5-35B-A3B adapter is a guess.** We haven't run it; `standard only` is the pessimistic default. First run may produce score=0 or validation retries, requiring a fallback added to its chain. That's fine — the whole point of the architecture is that adding one is a small, local change.
- **LFM2 has never been run end-to-end.** Its "bare name/arguments" shape is documented in the plan but never verified on PROD. Initial runs may reveal additional shapes.
- **Model-string drift risk.** If LM Studio rename cadence accelerates, the "add a new registry entry" policy becomes tedious. If we hit that pain, revisit normalization.
- **ModelProfile god-object risk.** Documented trigger for splitting (>8 fields or per-knob override needed) in §2.1. Revisit when a caller complains.
- **Salvage helper migration is behavior-preserving, not behavior-improving.** Step 5 does not fix gpt-oss-20b's existing failures; it just isolates them. GLM's fix is separate (no salvage = correct for GLM).

## 10. What this spec does NOT do

- Does not change the frontier path.
- Does not change the agent loop, validator, classifier, orchestrator, or any skill.
- Does not introduce new dependencies.
- Does not redesign the existing `NextStep` schema or tool catalog.
- Does not add per-call-site profile overrides (e.g., classifier-specific `reasoning_effort`). Documented as a trigger for a future profile split, not an initial feature.
