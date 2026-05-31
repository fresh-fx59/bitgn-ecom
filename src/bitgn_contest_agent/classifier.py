"""Shared lightweight LLM classifier for tier-2 routing decisions.

Both the pre-task router and reactive router use the same classifier
model (claude-haiku-4-5 via cliproxyapi) with the same confidence
threshold and JSON response format.  This module provides the shared
plumbing so neither router duplicates the OpenAI client factory,
prompt construction, or response parsing.

Callers build a system prompt and user message specific to their
routing context, then call ``classify()`` which returns the parsed
JSON dict.  Any failure raises; callers are expected to catch and
degrade gracefully (UNKNOWN / no-injection).

Retry logic: on JSON parse failure, the classifier first asks the
model to fix the broken JSON (cheap).  If that also fails, it retries
the full classification from scratch.  Max attempts controlled via
``BITGN_CLASSIFIER_MAX_ATTEMPTS`` (default 3).
"""
from __future__ import annotations

import json as _json
import logging
import os
import re as _re
import threading
import time as _time
from typing import TYPE_CHECKING, Any, List, Optional

import openai as _openai
from pydantic import BaseModel

from bitgn_contest_agent import router_config

# Transient error substrings from the aux route (linkapi / cliproxyapi).
# These strings appear in HTTP 400 responses that are gateway-side glitches,
# not caller mistakes, so they should be retried.  Mirrors the list in
# backend/openai_compat.py::_TRANSIENT_MESSAGE_SUBSTRINGS.
_AUX_RETRY_SUBSTRINGS = (
    "bad response status code 400",
    "bad_response_status_code",
    "upstream request failed",
    "upstream_connection_error",
    "concurrency limit exceeded",
)


def _is_retryable_aux_error(exc: Exception) -> bool:
    """Return True if *exc* is an openai API error with a transient message.

    Only ``openai.APIError`` subclasses (including ``BadRequestError``) are
    candidates; plain Python exceptions are never retried.
    """
    if isinstance(exc, _openai.APIError):
        msg = str(getattr(exc, "message", "") or exc).lower()
        return any(s in msg for s in _AUX_RETRY_SUBSTRINGS)
    return False


def _call_with_retry(fn, *, attempts: int = 3, on_attempt=None) -> Any:
    """Call *fn()* up to *attempts* times, retrying on transient aux errors.

    Raises immediately on any non-retryable exception.  If all attempts are
    exhausted the last retryable exception is re-raised.  No sleep between
    attempts — linkapi 400s are immediate gateway glitches, not rate limits.

    ``on_attempt`` (if given) is invoked with the 1-based attempt number
    just before each transport try, so the caller can record the true
    number of transport attempts in the aux_call trace event (including the
    single attempt made for a non-retryable failure). Purely observational.
    """
    last: Exception | None = None
    for i in range(attempts):
        if on_attempt is not None:
            on_attempt(i + 1)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable_aux_error(exc):
                raise
            last = exc
    raise last  # type: ignore[misc]

if TYPE_CHECKING:
    from bitgn_contest_agent.backend.base import Backend

_LOG = logging.getLogger(__name__)

_inflight_semaphore: threading.Semaphore | None = None

# ── Per-task aux-call coverage counters (thread-local) ──────────────────────
# Each agent task runs in its own thread (ThreadPoolExecutor in cli.py).
# Thread-local storage gives correct per-task attribution without any
# coupling between the aux-call sites (validator, judge_enforcer,
# task_canonicalizer, router, reactive_router) and the AgentLoop.

_aux_coverage = threading.local()


def _aux_cov() -> threading.local:
    cov = _aux_coverage
    if not hasattr(cov, "attempted"):
        cov.attempted = 0
        cov.succeeded = 0
        cov.failures_by_type = {}
        cov.purpose = None
    return cov


def reset_aux_coverage() -> None:
    """Zero the calling thread's aux-call counters (call at task start)."""
    cov = _aux_cov()
    cov.attempted = 0
    cov.succeeded = 0
    cov.failures_by_type = {}
    cov.purpose = None


def get_aux_coverage() -> tuple[int, int]:
    """Return (attempted, succeeded) aux LLM calls for the calling thread."""
    cov = _aux_cov()
    return cov.attempted, cov.succeeded


def get_aux_failures_by_type() -> dict[str, int]:
    """Return a copy of this thread's aux-failure histogram keyed by the
    exception class name (e.g. {"BadRequestError": 50}). Empty when no aux
    call has failed. Used to surface a run-level blackout in the outcome."""
    cov = _aux_cov()
    return dict(getattr(cov, "failures_by_type", {}) or {})


def set_aux_purpose(purpose: Optional[str]) -> Optional[str]:
    """Tag subsequent aux calls on this thread with *purpose* (one of
    classify / normalise / judge / ref_judge / completion). Returns the
    previous purpose so callers can restore it. Purely observational — it
    only labels the emitted aux_call trace event."""
    cov = _aux_cov()
    prev = getattr(cov, "purpose", None)
    cov.purpose = purpose
    return prev


def set_inflight_semaphore(sem: threading.Semaphore | None) -> None:
    """Set the shared inflight semaphore for classifier LLM calls.

    Called once by the CLI before launching parallel agents so that
    classifier calls (router, validator triggers) respect the same
    concurrency cap as the main agent LLM calls.
    """
    global _inflight_semaphore
    _inflight_semaphore = sem


def classify(*, system: str, user: str) -> Any:
    """Call the classifier model and return the parsed JSON response.

    Retry strategy per attempt:
    1. Send classification request → strip fences → parse JSON.
    2. If JSON parse fails, ask the model to fix the broken output.
    3. If the fix also fails, retry from step 1 (fresh classification).

    Max attempts controlled by ``router_config.classifier_max_attempts()``.

    Escape hatch: ``BITGN_SKIP_CLASSIFIER=1`` raises immediately without
    an HTTP call so the caller degrades to UNKNOWN. Needed when the
    classifier model is a slow local LLM (e.g. GLM-4.7-Flash) because LM
    Studio's MLX runtime does not cancel in-flight generation when the
    client disconnects — a timed-out classifier call keeps running in
    the background and queues every subsequent request behind it, even
    though httpx already reported timeout.

    Raises:
        The last exception encountered if all attempts are exhausted.
    """
    if os.environ.get("BITGN_SKIP_CLASSIFIER", "").strip() in {"1", "true", "True"}:
        raise RuntimeError("classifier skipped via BITGN_SKIP_CLASSIFIER=1")
    max_attempts = router_config.classifier_max_attempts()
    client = _get_openai_client()
    model = router_config.classifier_model()
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        # --- Phase 1: fresh classification ---
        content = ""
        try:
            content = _stream_call_content(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=_classifier_timeout_sec(),
            )
            if not content:
                last_error = ValueError("classifier returned empty content (None)")
                _LOG.warning("classify attempt %d: empty content, retrying", attempt + 1)
                continue

            cleaned = _strip_markdown_fences(content)
            return _json.loads(cleaned)

        except _json.JSONDecodeError as exc:
            _LOG.warning(
                "classify attempt %d: JSON parse failed at char %d: %s",
                attempt + 1, exc.pos, exc.msg,
            )
            last_error = exc

            # --- Phase 2: ask model to fix the broken JSON ---
            fixed = _try_fix_json(client, model, content, exc)
            if fixed is not None:
                return fixed

    raise last_error  # type: ignore[misc]


def _try_fix_json(
    client: Any,
    model: str,
    broken_output: str,
    error: _json.JSONDecodeError,
) -> Any | None:
    """Ask the model to fix broken JSON. Returns parsed dict or None."""
    fix_prompt = (
        f"Your previous response was not valid JSON. "
        f"Parse error at line {error.lineno}, column {error.colno}: {error.msg}\n\n"
        f"Your broken output was:\n{broken_output}\n\n"
        f"Return ONLY the corrected JSON object, no markdown fences, no explanation."
    )
    try:
        fix_content = _stream_call_content(
            client,
            model=model,
            messages=[{"role": "user", "content": fix_prompt}],
            timeout=_classifier_timeout_sec(),
        )
        if not fix_content:
            return None
        return _json.loads(_strip_markdown_fences(fix_content))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("JSON fix attempt failed: %s", exc)
        return None


def build_category_list(categories: List[str], *, fallback: str = "UNKNOWN") -> str:
    """Format a category list for a classifier system prompt.

    Returns a newline-separated bulleted list with a fallback entry.
    """
    lines = [f"- {c}" for c in categories]
    lines.append(f"- {fallback} (none of the above apply confidently)")
    return "\n".join(lines)


def parse_response(
    raw: Any,
    *,
    valid_categories: set[str],
) -> tuple[str | None, float]:
    """Extract (category, confidence) from a classifier JSON response.

    Returns ``(None, confidence)`` if the category is missing, not a
    string, or not in ``valid_categories``.
    """
    if not isinstance(raw, dict):
        return None, 0.0

    category = raw.get("category")
    confidence = raw.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    if not isinstance(category, str) or category not in valid_categories:
        return None, confidence

    return category, confidence


# ── Structured classification via Backend.call_structured ────────────


class ClassificationResult(BaseModel):
    """Schema for structured classification responses.

    Used with ``Backend.call_structured`` which forces valid JSON output
    via ``response_format=<schema>`` — eliminates the free-text JSON
    parse failures that plague small local models.
    """
    category: str
    confidence: float = 1.0


def classify_structured(
    backend: Backend,
    *,
    system: str,
    user: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Classify using ``Backend.call_structured`` with enforced JSON schema.

    Returns a dict matching the same shape as ``classify()`` so callers
    can use ``parse_response()`` on the result without changes.

    The system and user prompts are merged into a single user message
    because ``call_structured`` takes a single prompt string, not a
    message list.
    """
    if timeout_sec is None:
        timeout_sec = _classifier_timeout_sec()
    prompt = f"{system}\n\n---\n\n{user}"
    sem = _inflight_semaphore
    if sem is not None:
        with sem:
            result = backend.call_structured(
                prompt, ClassificationResult, timeout_sec=timeout_sec,
            )
    else:
        result = backend.call_structured(
            prompt, ClassificationResult, timeout_sec=timeout_sec,
        )
    return result.model_dump()


def _classifier_timeout_sec() -> float:
    """Per-call HTTP timeout for classifier LLM calls.

    Default 10s suits hosted Haiku via cliproxy; local 20B models under
    parallelism need much more headroom. Overridable via
    ``BITGN_CLASSIFIER_TIMEOUT_SEC``.
    """
    raw = os.environ.get("BITGN_CLASSIFIER_TIMEOUT_SEC", "10")
    try:
        return float(raw)
    except ValueError:
        return 10.0


def _llm_call(client: Any, **kwargs: Any) -> Any:
    """Make an OpenAI chat completion call, respecting the inflight semaphore.

    Wraps the transport call in ``_call_with_retry`` so transient aux-route
    400s (e.g. linkapi "bad response status code 400") are transparently
    retried up to 3 times before propagating.  The semaphore wraps the
    entire retry block so every attempt counts against the concurrency cap.

    Increments the thread-local aux-coverage counters: ``attempted`` once
    at entry (regardless of internal retries), ``succeeded`` only when the
    call returns without raising.  If ``_call_with_retry`` exhausts all
    attempts and raises, ``succeeded`` is NOT incremented and the failure
    is tallied in ``failures_by_type`` keyed by the exception class name.

    Observability (2026-05-31): on every logical aux call this emits one
    ``aux_call`` trace event (model, purpose, transport attempts, ok, and
    on failure the error TYPE + truncated body) to the task's TraceWriter
    if one is installed. Emitting NEVER changes the call's result — any
    exception from the transport still propagates unchanged.
    """
    cov = _aux_cov()
    cov.attempted += 1
    attempts_made = 0

    def _note_attempt(n: int) -> None:
        nonlocal attempts_made
        attempts_made = n

    def _do() -> Any:
        sem = _inflight_semaphore
        if sem is not None:
            with sem:
                return _call_with_retry(
                    lambda: client.chat.completions.create(**kwargs),
                    on_attempt=_note_attempt,
                )
        return _call_with_retry(
            lambda: client.chat.completions.create(**kwargs),
            on_attempt=_note_attempt,
        )

    started = _time.monotonic()
    try:
        result = _do()
    except Exception as exc:  # noqa: BLE001
        err_type = type(exc).__name__
        cov.failures_by_type = dict(getattr(cov, "failures_by_type", {}) or {})
        cov.failures_by_type[err_type] = cov.failures_by_type.get(err_type, 0) + 1
        _emit_aux_call_trace(
            model=kwargs.get("model", "?"),
            attempts=attempts_made or 1,
            ok=False,
            error_type=err_type,
            error_msg=str(getattr(exc, "message", "") or exc),
            latency_ms=int((_time.monotonic() - started) * 1000),
        )
        raise
    cov.succeeded += 1
    _emit_aux_call_trace(
        model=kwargs.get("model", "?"),
        attempts=attempts_made or 1,
        ok=True,
        latency_ms=int((_time.monotonic() - started) * 1000),
    )
    return result


def _emit_aux_call_trace(
    *,
    model: str,
    attempts: int,
    ok: bool,
    error_type: Optional[str] = None,
    error_msg: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> None:
    """Write an aux_call trace event to the task's writer, if installed.

    Best-effort and fully isolated: any failure here is swallowed so trace
    instrumentation can never break an aux call. The purpose is read from
    the per-thread coverage tag set by ``set_aux_purpose``."""
    try:
        from bitgn_contest_agent.arch_log import current_writer

        writer = current_writer()
        if writer is None:
            return
        writer.append_aux_call(
            model=model,
            purpose=getattr(_aux_cov(), "purpose", None),
            attempts=attempts,
            ok=ok,
            error_type=error_type,
            error_msg=error_msg,
            latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001  — observability must never break a call
        pass


_NON_REASONING_MODEL_MARKERS = (
    "gpt-4.1", "gpt-4o", "gpt-4-turbo", "gpt-4-", "gpt-3.5",
)


def _model_supports_reasoning(model: str) -> bool:
    """Whether ``model`` accepts the ``reasoning`` / ``reasoning_effort``
    request args.

    Reasoning models (gpt-5.x, o-series, Claude 4.x via the proxies) accept
    them; plain chat models (gpt-4.1*, gpt-4o*, gpt-4-*, gpt-3.5*) 400 with
    ``Unrecognized request arguments supplied: reasoning, reasoning_effort``
    on linkapi. Unknown ids default to True (reasoning-capable) so only the
    known plain-chat families are stripped — never the historic default.
    """
    m = (model or "").lower()
    return not any(tok in m for tok in _NON_REASONING_MODEL_MARKERS)


def _stream_call_content(
    client: Any,
    *,
    model: str,
    messages: list[dict],
    timeout: float,
    extra_body: dict | None = None,
) -> str:
    """Stream-mode chat completion helper that returns the concatenated
    content. Mandatory for cliproxyapi: its non-streaming chat.completions
    path drops `message.content` (returns null) for every reasoning model
    in its catalog. Streaming concatenates deltas correctly.

    Sends reasoning_effort in both shapes (flat + nested) ONLY for
    reasoning-capable models — plain chat models reject the args. See
    backend/openai_compat.py for the dual-shape rationale.

    Falls back to message.content read if the mock/test classifier
    returns a non-iterable (e.g. a completion object).
    """
    effort = os.environ.get("BITGN_CLASSIFIER_REASONING_EFFORT", "low").strip() or "low"
    body = dict(extra_body or {})
    # Only reasoning-capable models accept these args; plain chat models
    # (gpt-4.1-mini etc.) 400 on them. effort=none/off/0 disables globally.
    if effort.lower() not in ("none", "off", "0") and _model_supports_reasoning(model):
        body.setdefault("reasoning", {"effort": effort})
        body.setdefault("reasoning_effort", effort)
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=0.0,
        timeout=timeout,
        stream=True,
        stream_options={"include_usage": True},
        extra_body=body,
    )
    stream = _llm_call(client, **kwargs)
    parts: list[str] = []
    try:
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            piece = getattr(delta, "content", None) if delta else None
            if piece:
                parts.append(piece)
    except TypeError:
        # Test-only path: mock returned a chat completion object.
        choices = getattr(stream, "choices", None) or []
        if choices:
            content = getattr(getattr(choices[0], "message", None), "content", None)
            if content:
                parts.append(content)
    return "".join(parts)


def raw_completion(*, prompt: str, system: str | None = None,
                   timeout: float | None = None) -> str:
    """Single-shot completion that returns the text body, no JSON parsing.

    Used by task_canonicalizer + judge_enforcer. Bypasses the classify()
    retry loop because callers handle fallback themselves.

    Uses streaming mode: cliproxyapi's non-streaming chat.completions path
    drops `message.content` (returns null) for every reasoning model
    tested; streaming concatenates deltas correctly. Same pattern the
    agent's openai_compat backend uses.

    Raises on transport failure; never raises on empty body (returns "").
    """
    client = _get_openai_client()
    model = router_config.classifier_model()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _stream_call_content(
        client,
        model=model,
        messages=messages,
        timeout=timeout or _classifier_timeout_sec(),
    )


_FENCE_RE = _re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", _re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON (e.g. from Claude models)."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


_OPENAI_CLIENT_CACHE: dict[tuple[str, str], Any] = {}
_OPENAI_CLIENT_CACHE_LOCK = threading.Lock()


def _get_openai_client():  # pragma: no cover — thin factory, tested via patching
    """Return a process-cached OpenAI client.

    Each call to `OpenAI(...)` spawns a fresh `httpx.Client` with its own
    connection pool — across the bench's 100+ classifier calls per task,
    that's hundreds of TCP/TLS handshakes against the gateway. Cache the
    client by (base_url, api_key) so the same connection pool is reused.

    Thread-safe via lock; safe to call from parallel worker threads.
    """
    from openai import OpenAI
    base = os.environ.get("CLIPROXY_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    key = os.environ.get("CLIPROXY_API_KEY") or os.environ.get("OPENAI_API_KEY", "sk-proxy")
    cache_key = (base, key)
    with _OPENAI_CLIENT_CACHE_LOCK:
        client = _OPENAI_CLIENT_CACHE.get(cache_key)
        if client is not None:
            return client
        # max_retries=0: an httpx timeout here should fail the classifier and
        # degrade to UNKNOWN, not silently retry. SDK retries on local-LLM
        # timeouts just queue another generation on LM Studio's busy slot.
        client = OpenAI(
            base_url=base or None,
            api_key=key,
            max_retries=0,
        )
        _OPENAI_CLIENT_CACHE[cache_key] = client
        return client
