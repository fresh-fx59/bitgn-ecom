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
from typing import TYPE_CHECKING, Any, List

from pydantic import BaseModel

from bitgn_contest_agent import router_config

if TYPE_CHECKING:
    from bitgn_contest_agent.backend.base import Backend

_LOG = logging.getLogger(__name__)

_inflight_semaphore: threading.Semaphore | None = None


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
        try:
            resp = _llm_call(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                timeout=_classifier_timeout_sec(),
            )
            content = resp.choices[0].message.content
            if content is None:
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
        resp = _llm_call(
            client,
            model=model,
            messages=[{"role": "user", "content": fix_prompt}],
            temperature=0.0,
            timeout=_classifier_timeout_sec(),
        )
        fix_content = resp.choices[0].message.content
        if fix_content is None:
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
    """Make an OpenAI chat completion call, respecting the inflight semaphore."""
    sem = _inflight_semaphore
    if sem is not None:
        with sem:
            return client.chat.completions.create(**kwargs)
    return client.chat.completions.create(**kwargs)


_FENCE_RE = _re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", _re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON (e.g. from Claude models)."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _get_openai_client():  # pragma: no cover — thin factory, tested via patching
    from openai import OpenAI
    # max_retries=0: an httpx timeout here should fail the classifier and
    # degrade to UNKNOWN, not silently retry. SDK retries on local-LLM
    # timeouts just queue another generation on LM Studio's busy slot.
    return OpenAI(
        base_url=os.environ.get("CLIPROXY_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("CLIPROXY_API_KEY") or os.environ.get("OPENAI_API_KEY", "sk-proxy"),
        max_retries=0,
    )
