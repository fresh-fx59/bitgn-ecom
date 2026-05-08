# LM Studio watchdog — per-request timeout guard

Date: 2026-04-22
Status: approved, implementing

## Problem

LM Studio continues generating tokens after the OpenAI HTTP client
disconnects. On 2026-04-20 (PROD task t012), our `llm_http_timeout_sec=600`
client raised timeout while LM Studio chewed past 120k tokens on the GPU
slot. The `max_completion_tokens=100_000` cap we ship in the qwen adapter
is advertised on the wire but LM Studio does not enforce it in every
path. Subsequent requests queue behind the abandoned generation (13
queued observed 2026-04-21), wedging the slot.

OpenAI-compat `stopGenerating()` without a `request_id` is a no-op as of
LM Studio 0.3.31b4 ("Taking no action" log line).

## Goal

When a single LM Studio request overruns its client-side HTTP timeout by
more than a short grace, forcibly stop generation on the server so the
GPU slot is freed for the existing retry wrapper.

## Scope

**In.**
- LM Studio backends only: `openai/gpt-oss-20b`, `gpt-oss-120b`,
  `glm-4.7-flash-mlx`, `liquid/lfm2-24b-a2b`, `qwen3.5-35b-a3b`.
- Both call sites: `next_step` (`chat.completions.create`) and
  `call_structured` (`beta.chat.completions.parse`).
- Trigger: per-call wallclock > `llm_http_timeout_sec - 10s`. The grace
  is **subtracted**, not added: the watchdog must fire inside the
  `with`-block window, before the HTTP client raises. If the deadline
  sits past the HTTP timeout, httpx's timeout exception exits the
  context manager — which cancels the Timer — before the unload thread
  can run. Learned the hard way on the 2026-04-22 first PROD launch.
- Remediation: `lms.Client(host).llm.unload(model)` via lmstudio-python.
- Standalone operator CLI: `scripts/lmstudio_unload.py <host> <model>`.

**Out.**
- Remote qwen3.6 via neuraldeep LiteLLM gateway (no LM Studio, no unload).
- Token-count triggers, streaming, per-request cancellation, partial
  response recovery.
- Task-level watchdog in agent.py — redundant with the per-call one
  (`llm_http_timeout_sec + 10s ≤ task_timeout_sec` for every adapter).
- Queue introspection or listing other apps' predictions — no SDK API
  and unsafe.

## Architecture

**Single-tenant assumption.** `unload()` is global per model on that LM
Studio instance. Every fire logs at WARNING; operator is responsible for
keeping peer apps off the benchmark host.

**Module:** `src/bitgn_contest_agent/backend/lmstudio_watchdog.py`.
Context-manager shape, one `threading.Timer` per call, no registry.

```python
@contextmanager
def guard(*, request_id, model, host, deadline_sec) -> Iterator[None]:
    timer = threading.Timer(deadline_sec, _fire_unload,
                            args=(request_id, model, host))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()

def force_unload(host, model) -> None:
    lms.Client(host).llm.unload(model)
```

`_fire_unload` logs WARNING, calls `lms.Client(host).llm.unload(model)`,
swallows any unload exception (best-effort; the HTTP timeout has already
surfaced to the caller).

**LM Studio detection.** One new field on `ModelProfile`:

```python
lmstudio_host: str | None = None  # e.g. "localhost:1236"
```

`None` on `QwenA3bRemoteAdapter`; `"localhost:1236"` on the four LM Studio
adapters. The backend wraps `create()` with `guard(...)` only when the
field is set. Remote path is untouched.

**Integration.** Both call sites in `openai_toolcalling.py` become:

```python
host = self._adapter.profile.lmstudio_host
if host is not None:
    deadline = max(self._adapter.profile.llm_http_timeout_sec - 10.0, 5.0)
    with lmstudio_watchdog.guard(
        request_id=uuid.uuid4().hex[:8],
        model=self._model,
        host=host,
        deadline_sec=deadline,
    ):
        completion = self._client.chat.completions.create(**request_kwargs)
else:
    completion = self._client.chat.completions.create(**request_kwargs)
```

## Retry interaction

`_call_backend_with_retry` (`agent.py:898`) is untouched. On watchdog
fire:

1. Watchdog Timer runs at `llm_http_timeout_sec - 10s` while the HTTP
   call is still in flight. Unload drops the LM Studio slot.
2. The HTTP client sees the connection drop (or its own timeout fires
   ~10s later). The `with`-block exits via exception; Timer.cancel()
   on an already-fired timer is a no-op.
3. The retry wrapper sees the exception and backs off.
4. LM Studio cold-reloads the model on the retried call (~9s for
   qwen3.5). The slot is cleanly free.
5. No partial response is recovered. If the input itself is what caused
   the runaway (reasoning storm on an UNKNOWN-category task), the retry
   may reproduce the timeout — but the slot is freed each time, so the
   cohort is not wedged.

## Observability

Three log events per request:

- `WATCHDOG armed rid=<8hex> model=<m> deadline=<s>s` — DEBUG
- `WATCHDOG disarmed rid=<8hex> elapsed=<s>s` — DEBUG
- `WATCHDOG FIRED rid=<8hex> model=<m> host=<h> — unloading` — WARNING

On unload failure: `WATCHDOG unload failed: <exc>` — ERROR (swallowed).

## Non-decisions (deferred)

- Per-request cancellation with a PredictionStream handle. SDK's
  `respond()`/`respond_stream()` do not surface `tool_calls`; `act()` has
  no cancel handle. Either path is a transport rewrite we decided not to
  take (see verification spikes in `scripts/spike_lmstudio_cancel.py` and
  `scripts/spike_lmstudio_tools.py`).
- Token-live tracking. Would require `stream=True` and a chunk-tap layer.
  Out of scope; wallclock alone catches the failure modes observed.

## Rollout

1. Land behind no flag — watchdog is on by default on LM Studio
   adapters. It cannot fire before `llm_http_timeout_sec + 10s`, which
   is the same wall-clock budget we already commit to.
2. First full qwen3.5-35b-a3b PROD run exercises it. Expect 0 fires on a
   healthy run; any fire is a real wedge we would have had anyway
   without the watchdog.
