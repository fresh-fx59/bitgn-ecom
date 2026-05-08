"""OpenAI-compatible backend (routes through cliproxyapi by default).

Two code paths:
- Structured output via client.beta.chat.completions.parse(response_format=NextStep)
- Manual-parse fallback via client.chat.completions.create + json_object mode

The agent's P3 pattern (validation retry with critique) covers any
ValidationError raised in the fallback path, so the fallback is not a
correctness risk.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence, TypeVar

import httpx
import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

_T = TypeVar("_T", bound=BaseModel)

from bitgn_contest_agent.backend.base import Backend, Message, NextStepResult, TransientBackendError
from bitgn_contest_agent.schemas import NextStep


def _build_payload(messages: Sequence[Message]) -> List[Dict[str, Any]]:
    """Flatten ``Message`` sequence into the chat-completions wire shape.

    The CoT-preservation design moves the T24 cliproxyapi/Codex constraint
    (tool results must ride as ``role="user"`` to sidestep the
    ``function_call_output`` translator that demands a matching ``call_id``)
    from the agent loop into this backend. ``reasoning`` and ``tool_calls``
    on the incoming ``Message`` are intentionally ignored — this backend
    never populates them, and frontier models handle CoT internally.
    """
    payload: List[Dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            payload.append(
                {"role": "user", "content": f"Tool result:\n{m.content or ''}"}
            )
        else:
            payload.append({"role": m.role, "content": m.content or ""})
    return payload


_TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
    # T24 observation: httpx.ReadTimeout escapes the openai SDK when it is
    # raised while iterating a streaming response, because the SDK's retry
    # wrapper only covers the initial request, not the response body. Treat
    # it as transient so the P2 backoff helper catches it.
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


# Message-substring fallback: cliproxyapi (our localhost LLM proxy) can
# flatten an upstream socket error into a bare `openai.APIError` (the base
# class), rather than the narrower `APIConnectionError` that the SDK raises
# when it sees the socket error directly. Pattern-match the message so
# those blips still hit the P2 backoff retry.
# Verified from PROD t009 2026-04-15 crash:
#   "APIError: read tcp [...]:53210->[...]:443: read: connection reset by peer"
_TRANSIENT_MESSAGE_SUBSTRINGS: tuple[str, ...] = (
    "connection reset",     # TCP RST
    "econnreset",           # Connection reset by peer
    "broken pipe",          # EPIPE error
    "epipe",                # EPIPE variant
    "stream error",         # HTTP/2 RST_STREAM / GOAWAY — PROD t093 2026-04-20
)


def _is_transient_by_message(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(sub in msg for sub in _TRANSIENT_MESSAGE_SUBSTRINGS)


def _unwrap_schema_envelope(raw: str, schema: type[BaseModel]) -> str:
    """Unwrap a `{"<SchemaName>": {...}}` envelope if the model wrapped
    its structured response in one.

    Observed on cliproxyapi-backed gpt-5.4 during streaming-fallback
    mode for preflight_unknown (smoke test 2026-04-16): the model
    emitted `{"Rsp_PreflightUnknown": {"likely_class": ..., ...}}`
    instead of the bare object, and Pydantic rejected it for missing
    required fields. Peel exactly one level when the top-level dict has
    exactly one key whose name matches the schema class. Otherwise pass
    through unchanged so legitimate single-key shapes aren't corrupted.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return raw
    if (
        isinstance(obj, dict)
        and len(obj) == 1
        and schema.__name__ in obj
        and isinstance(obj[schema.__name__], dict)
    ):
        return json.dumps(obj[schema.__name__])
    return raw


def _extract_json_object(raw: str) -> str:
    """Pull the first top-level JSON object out of a model reply.

    Defensive: even when the system prompt says "return only JSON, no
    markdown fences", models sometimes emit ```json ... ``` wrappers or
    leading/trailing prose. Slice to the outermost brace pair so the
    Pydantic validator sees just the object. If no brace pair is found,
    return the raw string unchanged and let Pydantic raise its own
    ValidationError (the P3 path in AgentLoop handles it).
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip ```json or ``` opening fence (and optional language tag)
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    start = text.find("{")
    if start == -1:
        return text
    # Walk balanced braces, ignoring those inside string literals.
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class OpenAIChatBackend(Backend):
    def __init__(
        self,
        *,
        client: OpenAI,
        model: str,
        reasoning_effort: str,
        use_structured_output: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._use_structured_output = use_structured_output

    @classmethod
    def from_config(
        cls,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str,
    ) -> "OpenAIChatBackend":
        client = OpenAI(base_url=base_url, api_key=api_key)
        return cls(
            client=client,
            model=model,
            reasoning_effort=reasoning_effort,
            # T24 smoke test observation: cliproxyapi's upstream (codex via
            # gpt-5.3-codex) rejects structured output schemas that contain
            # `oneOf` nodes, which is exactly how Pydantic serializes the
            # discriminated FunctionUnion in NextStep. Plan A §9 open
            # question 5 predicted this — the fallback path uses json_object
            # mode and delegates validation to the P3 critique-injection
            # retry in AgentLoop, which is a correctness-equivalent path.
            use_structured_output=False,
        )

    def next_step(
        self,
        messages: Sequence[Message],
        response_schema: type[NextStep],
        timeout_sec: float,
    ) -> NextStepResult:
        payload = _build_payload(messages)
        try:
            if self._use_structured_output:
                completion = self._client.beta.chat.completions.parse(
                    model=self._model,
                    messages=payload,
                    response_format=response_schema,
                    timeout=timeout_sec,
                    extra_body={"reasoning": {"effort": self._reasoning_effort}},
                )
                parsed = completion.choices[0].message.parsed
                if parsed is None:
                    # Structured output mode returned no parsed value — fall
                    # back to parsing the raw content. Raises ValidationError
                    # on bad JSON, caught by the agent loop's P3 path.
                    raw = completion.choices[0].message.content or ""
                    parsed = response_schema.model_validate_json(raw)
                usage = getattr(completion, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                details = getattr(usage, "completion_tokens_details", None)
                reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0
                return NextStepResult(
                    parsed=parsed,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    reasoning_tokens=reasoning_tokens,
                )
            # T24 live-run observation: cliproxyapi drops message content on
            # the non-streaming chat-completions path (returns content: null
            # for every model in its model list, including gpt-5.3-codex,
            # gpt-5.4, gpt-5.1). Streaming mode concatenates deltas correctly.
            # We also drop `response_format: json_object` here because the
            # same routing path silently fails when it's set, and the system
            # prompt already tells the model to emit a NextStep JSON object.
            # P3 validation-retry in AgentLoop catches any non-JSON output.
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=payload,
                stream=True,
                stream_options={"include_usage": True},
                timeout=timeout_sec,
                extra_body={"reasoning": {"effort": self._reasoning_effort}},
            )
            parts: list[str] = []
            last_usage = None
            for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    last_usage = chunk_usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None) if delta else None
                if piece:
                    parts.append(piece)
            raw = _extract_json_object("".join(parts))
            parsed = response_schema.model_validate_json(raw)
            prompt_tokens = getattr(last_usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(last_usage, "completion_tokens", 0) or 0
            details = getattr(last_usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0
            return NextStepResult(
                parsed=parsed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
            )
        except _TRANSIENT_EXCEPTIONS as exc:
            raise TransientBackendError(str(exc)) from exc
        except openai.APIError as exc:
            # Fallback for proxy-flattened socket errors — see
            # _TRANSIENT_MESSAGE_SUBSTRINGS comment above.
            if _is_transient_by_message(exc):
                raise TransientBackendError(str(exc)) from exc
            raise
        except ValidationError:
            # Caller handles via P3 critique-injection retry.
            raise

    def call_structured(
        self,
        prompt: str,
        response_schema: type[_T],
        *,
        timeout_sec: float = 30.0,
    ) -> _T:
        """One-shot structured call — delegates to the same two paths as
        next_step (structured via beta.parse; streaming + manual validate
        otherwise)."""
        payload = [{"role": "user", "content": prompt}]
        try:
            if self._use_structured_output:
                completion = self._client.beta.chat.completions.parse(
                    model=self._model,
                    messages=payload,
                    response_format=response_schema,
                    timeout=timeout_sec,
                    extra_body={"reasoning": {"effort": self._reasoning_effort}},
                )
                parsed = completion.choices[0].message.parsed
                if parsed is None:
                    raw = completion.choices[0].message.content or ""
                    raw = _unwrap_schema_envelope(raw, response_schema)
                    parsed = response_schema.model_validate_json(raw)
                return parsed
            # Fallback: streaming + manual validate (same pattern as next_step).
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=payload,
                stream=True,
                stream_options={"include_usage": True},
                timeout=timeout_sec,
                extra_body={"reasoning": {"effort": self._reasoning_effort}},
            )
            parts: list[str] = []
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None) if delta else None
                if piece:
                    parts.append(piece)
            raw = _extract_json_object("".join(parts))
            raw = _unwrap_schema_envelope(raw, response_schema)
            return response_schema.model_validate_json(raw)
        except _TRANSIENT_EXCEPTIONS as exc:
            raise TransientBackendError(str(exc)) from exc
        except openai.APIError as exc:
            if _is_transient_by_message(exc):
                raise TransientBackendError(str(exc)) from exc
            raise
