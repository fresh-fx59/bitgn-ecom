"""Native OpenAI tool-calling backend for local models.

Used when ``AGENT_TOOLCALLING=1``. Each ``Req_*`` variant and
``ReportTaskCompletion`` is exposed as a separate OpenAI tool whose
parameter schema inlines the ``NextStep`` envelope (``current_state``,
``plan_remaining_steps_brief``, ``identity_verified``, ``observation``,
``outcome_leaning``) alongside the tool's own fields.

Why per-tool flat schemas rather than a single nested ``function``
discriminated union: the ``openai_compat`` backend ships with
``use_structured_output=False`` precisely because upstream (cliproxyapi /
Codex) rejects schemas containing ``oneOf`` nodes. Flat per-tool schemas
avoid ``oneOf`` entirely, which is exactly what LM Studio / llama.cpp
tool-calling implementations handle best.

The agent loop is unchanged: this backend still produces a ``NextStep``
and ``NextStepResult`` with the same fields as ``OpenAIChatBackend``. Only
the transport differs.
"""
from __future__ import annotations

import json as _json
import logging
import re as _re
import uuid as _uuid
from contextlib import nullcontext
from typing import Any, ContextManager, Dict, List, Optional, Sequence, Tuple, TypeVar

import httpx
import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

_T = TypeVar("_T", bound=BaseModel)

_LOG = logging.getLogger(__name__)

from bitgn_contest_agent.backend.base import (
    Backend,
    Message,
    NextStepResult,
    TransientBackendError,
)
from bitgn_contest_agent.backend import lmstudio_watchdog
from bitgn_contest_agent.schemas import (
    NextStep,
    REQ_MODELS,
    ReportTaskCompletion,
)


# Grace period subtracted from ``llm_http_timeout_sec``. The watchdog
# must fire BEFORE the HTTP client raises, otherwise the ``with`` block
# exits (via httpx's timeout exception), which cancels the Timer before
# its callback can run — unload never fires. Firing 10s early lets the
# unload run while the HTTP call is still in flight; the client's
# timeout then surfaces (or the unload itself drops the connection
# earlier), the retry wrapper sees the exception, and the next call
# hits a cleanly freed slot.
_WATCHDOG_GRACE_SEC: float = 10.0


def _watchdog_guard(adapter: Any, model: str) -> ContextManager[None]:
    """Return a watchdog guard CM when ``adapter`` targets LM Studio.

    For non-LM-Studio backends (remote qwen3.6 gateway), returns a
    no-op ``nullcontext`` so the call site can unconditionally ``with``.
    """
    host = getattr(adapter.profile, "lmstudio_host", None)
    if host is None:
        return nullcontext()
    deadline = max(
        adapter.profile.llm_http_timeout_sec - _WATCHDOG_GRACE_SEC,
        5.0,
    )
    return lmstudio_watchdog.guard(
        request_id=_uuid.uuid4().hex[:8],
        model=model,
        host=host,
        deadline_sec=deadline,
    )


_TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


_ENVELOPE_FIELDS: Tuple[str, ...] = (
    "current_state",
    "plan_remaining_steps_brief",
    "identity_verified",
    "observation",
    "outcome_leaning",
)

_OUTCOME_LEANING_VALUES: Tuple[str, ...] = (
    "GATHERING_INFORMATION",
    "OUTCOME_OK",
    "OUTCOME_DENIED_SECURITY",
    "OUTCOME_NONE_CLARIFICATION",
    "OUTCOME_NONE_UNSUPPORTED",
)

# Defaults injected when a small local model omits the envelope fields
# despite being listed in the tool schema. Keeps the NextStep valid and
# lets the validator operate without envelope-filling becoming a blocker
# on every turn. Good models (that fill the envelope) get the full
# benefit; sloppy models still drive the benchmark.
_ENVELOPE_DEFAULTS: Dict[str, Any] = {
    "current_state": "(not provided by model)",
    "plan_remaining_steps_brief": ["continue task"],
    "identity_verified": False,
    "observation": "(not provided by model)",
    "outcome_leaning": "GATHERING_INFORMATION",
}

# Matches the maxItems=5 constraint the schema fragment advertises.
# Sloppy local models routinely emit longer lists (e.g. one plan entry per
# file they intend to touch on a "delete all captured cards" task).
# Instead of losing the turn to too_long validation, keep the first 5.
_PLAN_MAX_ITEMS: int = 5


def _envelope_schema_fragment() -> Dict[str, Any]:
    """Return the JSONSchema object fragment shared by every tool.

    Properties + required subset the planner must fill before acting.
    """
    return {
        "current_state": {
            "type": "string",
            "minLength": 1,
            "description": "Your reasoning scratchpad — what's the state, what have you tried.",
        },
        "plan_remaining_steps_brief": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
            "description": "1-5 upcoming actions you plan to take.",
        },
        "identity_verified": {
            "type": "boolean",
            "description": "True only after AGENTS.md and context() have been read.",
        },
        "observation": {
            "type": "string",
            "minLength": 1,
            "description": "What this step revealed — a factual statement, not a plan.",
        },
        "outcome_leaning": {
            "type": "string",
            "enum": list(_OUTCOME_LEANING_VALUES),
            "description": "Current lean on the task outcome.",
        },
    }


def _tool_spec_for_req(model_cls: type) -> Dict[str, Any]:
    """Build one OpenAI tool spec from a Req_* pydantic model.

    The envelope fields are inlined as required parameters alongside the
    tool's own fields (everything except the ``tool`` discriminator).
    """
    raw = model_cls.model_json_schema()
    # Pull the Req_* own fields (minus the literal 'tool' discriminator)
    own_props: Dict[str, Any] = {}
    own_required: List[str] = []
    for name, schema in (raw.get("properties") or {}).items():
        if name == "tool":
            continue
        own_props[name] = schema
    for name in raw.get("required") or []:
        if name != "tool":
            own_required.append(name)

    envelope = _envelope_schema_fragment()
    combined_props: Dict[str, Any] = {**envelope, **own_props}
    # Envelope fields are advertised as properties on every tool so good
    # models fill them, but only the tool's own fields are REQUIRED. Small
    # local models routinely ignore ``required`` on every field, and we'd
    # rather default-fill the envelope than lose every turn to
    # double-validation failure.
    combined_required = own_required

    tool_name = raw.get("properties", {}).get("tool", {}).get("const") \
        or raw.get("properties", {}).get("tool", {}).get("enum", [None])[0]
    if tool_name is None:
        raise RuntimeError(f"cannot determine tool name for {model_cls!r}")

    description = (model_cls.__doc__ or raw.get("title") or tool_name).strip().splitlines()[0]
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": combined_props,
                "required": combined_required,
                "additionalProperties": False,
            },
        },
    }


def build_tool_catalog() -> List[Dict[str, Any]]:
    """Construct the full tool catalog sent on every request.

    Covers every ``Req_*`` action tool plus ``ReportTaskCompletion``.
    """
    catalog: List[Dict[str, Any]] = []
    for model_cls in REQ_MODELS:
        catalog.append(_tool_spec_for_req(model_cls))
    catalog.append(_tool_spec_for_req(ReportTaskCompletion))
    return catalog


def _split_envelope(
    args: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split a tool-call arguments dict into (envelope, tool_specific)."""
    env = {k: args[k] for k in _ENVELOPE_FIELDS if k in args}
    rest = {k: v for k, v in args.items() if k not in _ENVELOPE_FIELDS}
    return env, rest


def _build_next_step(tool_name: str, args: Dict[str, Any]) -> NextStep:
    """Construct a NextStep from a tool_call's (name, arguments).

    Envelope fields missing or empty in ``args`` are default-filled from
    ``_ENVELOPE_DEFAULTS``. This keeps small local models (which
    frequently ignore JSON-schema ``required`` on anything beyond the
    tool's own parameters) from losing every turn to validation failure.
    """
    env, rest = _split_envelope(args)
    for k, default in _ENVELOPE_DEFAULTS.items():
        val = env.get(k)
        if val is None or (isinstance(val, str) and val.strip() == "") \
                or (isinstance(val, list) and len(val) == 0):
            env[k] = default
    plan = env.get("plan_remaining_steps_brief")
    if isinstance(plan, list) and len(plan) > _PLAN_MAX_ITEMS:
        env["plan_remaining_steps_brief"] = plan[:_PLAN_MAX_ITEMS]
    leaning = env.get("outcome_leaning")
    if leaning not in _OUTCOME_LEANING_VALUES:
        env["outcome_leaning"] = _ENVELOPE_DEFAULTS["outcome_leaning"]
    function_payload = {"tool": tool_name, **rest}
    return NextStep.model_validate(
        {
            **env,
            "function": function_payload,
        }
    )


def _extract_first_json_object(text: str) -> Dict[str, Any] | None:
    """Find and parse the first balanced ``{...}`` JSON object in ``text``.

    Small local models sometimes wrap their JSON in prose or code fences.
    Scan for a brace-balanced object and attempt ``json.loads`` on it. If
    no scan reaches depth 0 (the response was cut mid-JSON by an
    upstream token cap), fall back to ``_repair_truncated_json`` on the
    suffix starting at the first ``{``.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = _json.loads(candidate)
                    except _json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    first = text.find("{")
    if first == -1:
        return None
    return _repair_truncated_json(text[first:])


def _repair_truncated_json(text: str) -> Dict[str, Any] | None:
    """Best-effort parse of a JSON object that was cut off mid-structure.

    Walks the input tracking string state, array/object depth, and escape
    sequences. On reaching the end with unclosed scopes, closes the open
    string (if any), drops any trailing ``,`` or ``:`` that would make
    the JSON invalid, closes any dangling partial-key, then appends the
    matching ``]``/``}`` closers in reverse order. If that yields a
    valid dict, returns it; otherwise ``None``.

    Only applied when the balanced-brace scanner found no complete
    object — the normal happy path is unchanged.
    """
    stack: List[str] = []
    in_str = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch == "}" or ch == "]":
            if stack and stack[-1] == ch:
                stack.pop()
    repaired = text
    if in_str:
        repaired += '"'
    stripped = repaired.rstrip()

    def _try(payload: str) -> Dict[str, Any] | None:
        try:
            obj = _json.loads(payload)
        except _json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    # Attempt 1: just close open scopes.
    attempt = stripped + "".join(reversed(stack))
    out = _try(attempt)
    if out is not None:
        return out

    # Attempt 2: drop a trailing ``,`` or ``:`` that would leave a dangling
    # pair, then close.
    while stripped and stripped[-1] in ",:":
        stripped = stripped[:-1].rstrip()
    attempt = stripped + "".join(reversed(stack))
    out = _try(attempt)
    if out is not None:
        return out

    # Attempt 3: walk back past the last ``,`` or ``{`` to drop a partial
    # or incomplete trailing pair entirely, then close.
    for cut in range(len(stripped) - 1, -1, -1):
        c = stripped[cut]
        if c == ',':
            candidate = stripped[:cut]
            break
        if c == '{':
            candidate = stripped[:cut + 1]
            break
    else:
        return None
    attempt = candidate + "".join(reversed(stack))
    return _try(attempt)


def _collect_valid_tool_names() -> frozenset[str]:
    names: List[str] = []
    for model_cls in REQ_MODELS:
        lit = model_cls.model_fields["tool"].annotation
        names.extend(getattr(lit, "__args__", ()))
    names.extend(
        getattr(ReportTaskCompletion.model_fields["tool"].annotation, "__args__", ())
    )
    return frozenset(names)


_VALID_TOOL_NAMES: frozenset[str] = _collect_valid_tool_names()


# Threshold for the salvage-miss circuit breaker in ``next_step``.
#
# When the model returns a content-only envelope loop (neither native
# ``tool_calls`` nor any JSON shape that ``_try_salvage_from_content`` can
# recover), we raise a ValidationError which becomes a critique on the
# next turn. A well-behaved model uses that critique; a stuck model
# ignores it. Two retries is the typical critique-retry budget before
# the step is a lost cause, so on the third consecutive miss we stop
# burning turns and synthesize a terminal
# ``report_completion(OUTCOME_NONE_UNSUPPORTED)`` instead.
_SALVAGE_MISS_CIRCUIT_BREAKER_THRESHOLD: int = 3


# gpt-oss harmony channel headers leak when LM Studio's parser doesn't
# route them. Shapes observed in v9-v12 PROD logs:
#
#   <|channel|>commentary to=functions.<TOOL> <|constrain|>json<|message|>{...}
#   <|channel|>final <|constrain|>commentary to=functions.<TOOL> <|constrain|>json<|message|>{...}
#   <|channel|>final <|constrain|><TOOL><|message|>{...}
#   <|channel|>final <|constrain|>json<|message|>{...}
#   <|channel|>final<|message|>{...}
#
# When a tool name is recoverable (via ``to=functions.<TOOL>`` or a bare
# ``<|constrain|><TOOL>`` where <TOOL> is a known valid tool), the JSON
# body is bare arguments for that tool. Otherwise the body is typically a
# full NextStep envelope. Strip the header (and any trailing
# ``<|end|>`` / ``<|return|>`` / ``<|call|>``), capture the tool name when
# present, and hand the remainder to the existing salvage shapes.
_HARMONY_FUNCTIONS_HEADER = _re.compile(
    r"<\|channel\|>.*?to=functions\.(?P<tool>[A-Za-z_][A-Za-z0-9_]*)"
    r".*?<\|message\|>",
    _re.IGNORECASE | _re.DOTALL,
)
# v13 shape seen in PROD task t009 (2026-04-15):
#   <|channel|>...<|constrain|>function: {"tool": "write", ...}<|message|>{body}
# The tool name lives inside a JSON-ish preamble keyed by "tool". Match
# AFTER ``_HARMONY_FUNCTIONS_HEADER`` (more specific: requires "tool":
# inside the preamble) but BEFORE the bare ``<|constrain|><TOOL>`` form.
_HARMONY_CONSTRAIN_FUNCTION_HEADER = _re.compile(
    r"<\|constrain\|>function:\s*\{.*?\"tool\"\s*:\s*"
    r"\"(?P<tool>[A-Za-z_][A-Za-z0-9_]*)\".*?<\|message\|>",
    _re.IGNORECASE | _re.DOTALL,
)
_HARMONY_CONSTRAIN_TOOL_HEADER = _re.compile(
    r"<\|channel\|>.*?<\|constrain\|>(?P<tool>[A-Za-z_][A-Za-z0-9_]*)"
    r"<\|message\|>",
    _re.IGNORECASE | _re.DOTALL,
)
_HARMONY_FINAL_HEADER = _re.compile(
    r"<\|channel\|>.*?<\|message\|>",
    _re.IGNORECASE | _re.DOTALL,
)
_HARMONY_END = _re.compile(r"<\|(?:end|return|call)\|>\s*$")


def _strip_harmony(content: str) -> Tuple[Optional[str], str]:
    """Return ``(tool_name, body)`` after stripping gpt-oss harmony tags.

    Tries, in order: an explicit ``to=functions.<TOOL>`` header, a
    ``<|constrain|>function: {"tool": "<TOOL>", ...}<|message|>`` JSON
    preamble (v13 shape), a bare ``<|constrain|><TOOL><|message|>``
    header where <TOOL> is a known valid tool name (not e.g. ``json``),
    then a generic channel header with no tool target. ``tool_name`` is
    the captured target (or ``None``); ``body`` is the content with the
    matched header and any trailing ``<|end|>`` / ``<|return|>`` /
    ``<|call|>`` sentinel stripped. If no header matched, ``body`` is
    returned unchanged.
    """
    if not content:
        return None, content
    m = _HARMONY_FUNCTIONS_HEADER.search(content)
    if m is not None:
        body = content[m.end():]
        body = _HARMONY_END.sub("", body).rstrip()
        return m.group("tool"), body
    m = _HARMONY_CONSTRAIN_FUNCTION_HEADER.search(content)
    if m is not None and m.group("tool") in _VALID_TOOL_NAMES:
        body = content[m.end():]
        body = _HARMONY_END.sub("", body).rstrip()
        return m.group("tool"), body
    m = _HARMONY_CONSTRAIN_TOOL_HEADER.search(content)
    if m is not None and m.group("tool") in _VALID_TOOL_NAMES:
        body = content[m.end():]
        body = _HARMONY_END.sub("", body).rstrip()
        return m.group("tool"), body
    m = _HARMONY_FINAL_HEADER.search(content)
    if m is not None:
        body = content[m.end():]
        body = _HARMONY_END.sub("", body).rstrip()
        return None, body
    return None, content


# Terminal non-OK outcomes the model can reach without calling a tool.
# OUTCOME_OK is deliberately excluded: claiming correctness requires a
# committed answer in ``message``, and an envelope-only reply has none.
# OUTCOME_ERR_INTERNAL is excluded because it's the internal-error sentinel
# the harness assigns on its own — the model shouldn't claim it.
_SALVAGE_TERMINAL_LEANINGS: frozenset[str] = frozenset({
    "OUTCOME_DENIED_SECURITY",
    "OUTCOME_NONE_CLARIFICATION",
    "OUTCOME_NONE_UNSUPPORTED",
})


def _maybe_salvage_envelope_terminal(obj: Dict[str, Any]) -> NextStep | None:
    """Synthesize a ``report_completion`` from an envelope-only reply.

    Triggers only when ``obj`` has a NextStep envelope (``outcome_leaning``
    present) with a terminal non-OK value, but no ``function`` or ``name``
    key identifying which tool to call. The model already telegraphed
    "I'm giving up with outcome X" — terminating the task here saves the
    turn-budget it would otherwise burn on critique/retry loops.

    Populates the six ``ReportTaskCompletion`` required fields from the
    envelope's ``current_state`` / ``observation`` so the synthesized call
    validates. Returns ``None`` for any other shape.
    """
    if "function" in obj or "name" in obj:
        return None
    leaning = obj.get("outcome_leaning")
    if leaning not in _SALVAGE_TERMINAL_LEANINGS:
        return None
    reason = (
        (isinstance(obj.get("current_state"), str) and obj["current_state"].strip())
        or (isinstance(obj.get("observation"), str) and obj["observation"].strip())
        or str(leaning)
    )
    merged: Dict[str, Any] = {
        **{k: obj[k] for k in _ENVELOPE_FIELDS if k in obj},
        "message": reason,
        "grounding_refs": [],
        "rulebook_notes": reason,
        "outcome_justification": reason,
        "completed_steps_laconic": [],
        "outcome": leaning,
    }
    try:
        return _build_next_step("report_completion", merged)
    except ValidationError:
        return None


def _try_salvage_from_content(content: str) -> NextStep | None:
    """Attempt to build a NextStep from a content-only reply.

    Four shapes to handle:
      1. gpt-oss harmony ``commentary to=functions.<TOOL>`` header with
         the JSON body being the bare arguments dict for ``<TOOL>``.
      2. ``{"name": "<tool>", "arguments": {...}}`` — bare OpenAI tool
         shape emitted as free text (liquid/lfm2 trained behavior).
      3. ``{"current_state": ..., "function": {"tool": ..., ...}}`` — the
         full NextStep envelope that the OpenAIChatBackend expects.
      4. Envelope-only with a terminal non-OK ``outcome_leaning`` (no
         ``function``/``name``): synthesize a ``report_completion`` from
         the telegraphed outcome + reasoning. Saves the turn budget the
         critique/retry loop would otherwise burn.

    Harmony headers (channel/final, with or without tool target) are
    stripped before shapes 2-4 are tried.

    Returns the parsed ``NextStep`` on success, ``None`` otherwise.
    """
    harmony_tool, body = _strip_harmony(content)

    # Bare-value reply: short content with no JSON braces and few words.
    # Local models sometimes emit a raw answer ("780", "Tobias") instead of
    # a tool call.  Synthesize report_completion so the answer isn't lost.
    # Guard: ≤80 chars and ≤5 words — keeps prose/confusion sentences out.
    stripped = body.strip()
    if (stripped and "{" not in stripped
            and len(stripped) <= 80 and len(stripped.split()) <= 5):
        try:
            return _build_next_step("report_completion", {
                "message": stripped,
                "outcome": "OUTCOME_OK",
                "outcome_justification": "bare-value salvage",
                "rulebook_notes": "—",
                "grounding_refs": [],
                "completed_steps_laconic": [],
            })
        except ValidationError:
            pass  # fall through to JSON extraction

    obj = _extract_first_json_object(body)
    if obj is None:
        return None
    if harmony_tool is not None and harmony_tool in _VALID_TOOL_NAMES:
        try:
            return _build_next_step(harmony_tool, obj)
        except ValidationError:
            pass
    if "name" in obj and isinstance(obj.get("arguments"), dict):
        tool_name = obj.get("name")
        if tool_name in _VALID_TOOL_NAMES:
            try:
                return _build_next_step(tool_name, obj["arguments"])
            except ValidationError:
                return None
    if "function" in obj and isinstance(obj["function"], dict):
        func = obj["function"]
        tool_name = func.get("tool")
        if tool_name in _VALID_TOOL_NAMES:
            merged: Dict[str, Any] = {}
            for key in _ENVELOPE_FIELDS:
                if key in obj:
                    merged[key] = obj[key]
            for key, val in func.items():
                if key != "tool":
                    merged[key] = val
            # Local models routinely emit empty strings for NonEmptyStr
            # fields (rulebook_notes, outcome_justification) when they mean
            # "nothing applicable". Strict schema validation would reject,
            # losing an otherwise-valid terminal. Inject a placeholder so
            # salvage succeeds — the agent's answer is what matters.
            for placeholder_field in (
                "rulebook_notes", "outcome_justification", "message",
            ):
                if merged.get(placeholder_field) == "":
                    merged[placeholder_field] = "—"
            from bitgn_contest_agent.backend.adapters._helpers import (
                _sanitize_grounding_refs,
            )
            _sanitize_grounding_refs(merged)
            try:
                return _build_next_step(tool_name, merged)
            except ValidationError:
                return None
    return _maybe_salvage_envelope_terminal(obj)


def _build_payload(messages: Sequence[Message]) -> List[Dict[str, Any]]:
    """Convert provider-agnostic ``Message`` sequence into the OpenAI wire shape.

    Branches by message shape per the CoT-preservation design:

    - ``role == "assistant"`` with ``tool_calls`` set → canonical
      OpenAI/LM Studio assistant-with-tool_calls payload:
      ``{"role": "assistant", "content": None, "tool_calls": [...], "reasoning": ...}``.
      The ``reasoning`` key is omitted when ``None`` so non-reasoning
      providers don't see a surprise field.
    - ``role == "tool"`` → ``{"role": "tool", "tool_call_id": <id>, "content": <result>}``.
    - Anything else (including salvage-path assistant turns that have
      ``content`` but no ``tool_calls``) → ``{"role": role, "content": content or ""}``.

    Replaces the inline ``[{"role": m.role, "content": m.content} for m in messages]``
    comprehension that used to live at the top of ``next_step``.
    """
    payload: List[Dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls is not None:
            entry: Dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": m.tool_calls,
            }
            if m.reasoning is not None:
                entry["reasoning"] = m.reasoning
            payload.append(entry)
        elif m.role == "tool":
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content or "",
                }
            )
        else:
            payload.append({"role": m.role, "content": m.content or ""})
    return payload


class OpenAIToolCallingBackend(Backend):
    """Backend that uses native OpenAI tool-calling instead of free-text JSON."""

    def __init__(
        self,
        *,
        client: OpenAI,
        model: str,
        reasoning_effort: str,
        adapter: Optional["Any"] = None,
    ) -> None:
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._tools = build_tool_catalog()
        # Per-backend-instance counter for the salvage-miss circuit
        # breaker. Scope is one ``OpenAIToolCallingBackend`` instance;
        # the agent lifecycle builds a fresh backend per task so this
        # does NOT leak across tasks.
        self._consecutive_salvage_misses: int = 0
        # Model adapter drives request shaping and response extraction.
        # Default falls back to the legacy gpt-oss full-chain salvage so
        # callers that don't inject an adapter (tests, direct ctor users)
        # keep working byte-identical to the pre-adapter backend.
        if adapter is None:
            from bitgn_contest_agent.backend.adapters.gpt_oss import GptOssAdapter
            adapter = GptOssAdapter()
        self._adapter = adapter

    @property
    def model_adapter(self):
        """Expose the per-model ``ModelAdapter`` for consumers (e.g. the
        agent loop) that need to call behavioral hooks like
        ``format_retry_critique`` or ``post_process_terminal``.

        Other ``Backend`` implementations (frontier ``OpenAIChatBackend``,
        test stubs) do not expose this attribute; ``agent.py`` reads it
        with ``getattr(..., None)`` so the default behavior path remains
        unchanged for non-toolcalling backends.
        """
        return self._adapter

    @classmethod
    def from_config(
        cls,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str,
    ) -> "OpenAIToolCallingBackend":
        from bitgn_contest_agent.backend.adapters import get_adapter

        # max_retries=0: the OpenAI SDK's default (2) multiplies the
        # per-request httpx timeout by 3x and, worse, each SDK retry
        # queues another generation on LM Studio's single slot (MLX
        # doesn't cancel in-flight gens when the client disconnects).
        # Local slow models (GLM reasoning) would thrash; the agent
        # loop's own backoff schedule handles transient errors at the
        # correct layer.
        client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
        adapter = get_adapter(model)
        return cls(
            client=client,
            model=model,
            reasoning_effort=reasoning_effort,
            adapter=adapter,
        )

    def next_step(
        self,
        messages: Sequence[Message],
        response_schema: type[NextStep],
        timeout_sec: float,
    ) -> NextStepResult:
        request_kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": _build_payload(messages),
            "tools": self._tools,
            "tool_choice": "required",
            "timeout": timeout_sec,
            # Per-adapter ceiling. 4096 for terse gpt-oss-style models;
            # ~100k for reasoning-heavy qwen-a3b where high-effort CoT can
            # run away on UNKNOWN-category tasks and chew wall-clock past
            # our HTTP timeout (LM Studio keeps generating after client
            # disconnect). See ``ModelProfile.max_completion_tokens``.
            "max_tokens": self._adapter.profile.max_completion_tokens,
            "extra_body": {"reasoning": {"effort": self._reasoning_effort}},
        }
        # Adapters that need outbound payload shaping (e.g. qwen's
        # output-discipline system nudge) mutate here. Default is a
        # passthrough so other adapters stay byte-identical.
        request_kwargs = self._adapter.shape_request(request_kwargs)
        try:
            with _watchdog_guard(self._adapter, self._model):
                completion = self._client.chat.completions.create(**request_kwargs)
        except _TRANSIENT_EXCEPTIONS as exc:
            raise TransientBackendError(str(exc)) from exc
        except openai.BadRequestError as exc:
            # LM Studio returns 400 to every in-flight request when its
            # model slot is temporarily unavailable:
            #   - "Model reloaded."  (weight swap, ~30-60s)
            #   - "The model has crashed without additional information."
            #     (OOM or server-side segfault; LM Studio restarts the
            #     slot on the next request, same recovery window)
            #   - "Model unloaded."  (watchdog force-unload on wall-clock
            #     overrun; the same HTTP call whose deadline the watchdog
            #     fired on receives this 400 instead of a connection drop)
            # All three are transient for the parallel cohort — reclassify
            # so the caller's retry loop waits out the recovery instead of
            # killing every in-flight task permanently.
            msg = str(exc).lower()
            if ("model reloaded" in msg or "model has crashed" in msg
                    or "model unloaded" in msg):
                raise TransientBackendError(str(exc)) from exc
            raise
        except openai.NotFoundError as exc:
            # The neuraldeep LiteLLM gateway transiently returns
            # ``404 page not found`` (HTML body, not a JSON API error)
            # when its upstream provider routing reloads — observed
            # 2026-05-01 qwen3.6 PROD run, 10× 404s spread across step 1
            # of t006/t011/t012/t013/t015/t016/t017 (lost ~7 tasks).
            # cliproxyapi config 404s carry a different body — typically
            # ``unknown provider for model <name>``, which is a real
            # config bug and must NOT retry. Pattern-match the body to
            # split the two cases.
            msg = str(exc).lower()
            if "page not found" in msg:
                raise TransientBackendError(str(exc)) from exc
            raise

        choice = completion.choices[0]
        # Surface the cap-hit signal. When LM Studio hits ``max_tokens``
        # it returns ``finish_reason="length"`` with whatever it had
        # generated so far (often no tool_call, partial reasoning). The
        # salvage/retry layers handle the missing structure; we log the
        # cap hit at WARNING so operators can tell "agent gave up mid-
        # reasoning" apart from "model chose the wrong tool."
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            _LOG.warning(
                "llm completion hit max_tokens cap (model=%s, max_tokens=%d)",
                self._model,
                self._adapter.profile.max_completion_tokens,
            )
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        content = getattr(choice.message, "content", None) or ""
        # LM Studio 0.3.23+ surfaces gpt-oss CoT on ``message.reasoning``.
        # Fall back to ``model_dump()`` if the attribute is missing (older
        # SDK paths or pydantic-model messages where the field isn't in
        # the attribute namespace but appears in the dict form).
        reasoning_text: Optional[str] = getattr(choice.message, "reasoning", None)
        if reasoning_text is None:
            dump_fn = getattr(choice.message, "model_dump", None)
            if callable(dump_fn):
                try:
                    reasoning_text = dump_fn().get("reasoning")
                except Exception:  # noqa: BLE001 - defensive, never mask a response
                    reasoning_text = None
        tool_calls_raw: Optional[List[Dict[str, Any]]] = None
        if tool_calls:
            tool_calls_raw = []
            for tc in tool_calls:
                dumped: Optional[Dict[str, Any]] = None
                dump_fn = getattr(tc, "model_dump", None)
                if callable(dump_fn):
                    try:
                        maybe = dump_fn()
                    except Exception:  # noqa: BLE001 - defensive
                        maybe = None
                    if isinstance(maybe, dict):
                        dumped = maybe
                if dumped is None:
                    # Fallback for attribute-style shapes (e.g. test doubles
                    # that don't provide a real ``model_dump``).
                    fn = getattr(tc, "function", None)
                    dumped = {
                        "id": getattr(tc, "id", None),
                        "type": getattr(tc, "type", "function"),
                        "function": {
                            "name": getattr(fn, "name", None),
                            "arguments": getattr(fn, "arguments", None),
                        },
                    }
                tool_calls_raw.append(dumped)
        usage = getattr(completion, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0

        if not tool_calls:
            # LM Studio does not always honor ``tool_choice="required"``.
            # Delegate content-based fallback to the adapter; adapters that
            # know their model's content-only shapes will salvage (gpt-oss,
            # lfm2). Adapters that know salvage is unsafe (glm: chat-template
            # leakage) return None and the critique/retry loop handles it.
            salvaged = self._adapter.extract_next_step(choice.message)
            if salvaged is not None:
                parsed = salvaged
                # Successful salvage resets the circuit breaker.
                self._consecutive_salvage_misses = 0
            else:
                self._consecutive_salvage_misses += 1
                if (
                    self._consecutive_salvage_misses
                    >= _SALVAGE_MISS_CIRCUIT_BREAKER_THRESHOLD
                ):
                    misses = self._consecutive_salvage_misses
                    _LOG.warning(
                        "circuit_breaker_fired: consecutive salvage "
                        "misses=%d, synthesizing OUTCOME_NONE_UNSUPPORTED "
                        "terminal",
                        misses,
                    )
                    breaker_message = (
                        f"Circuit breaker: {misses} consecutive "
                        "salvage_miss; model not emitting parseable tool "
                        "calls."
                    )
                    synthesized = _build_next_step(
                        "report_completion",
                        {
                            **_ENVELOPE_DEFAULTS,
                            "outcome": "OUTCOME_NONE_UNSUPPORTED",
                            "outcome_leaning": "OUTCOME_NONE_UNSUPPORTED",
                            "message": breaker_message,
                            "grounding_refs": [],
                            "rulebook_notes": breaker_message,
                            "outcome_justification": breaker_message,
                            "completed_steps_laconic": [],
                        },
                    )
                    # Reset counter so a subsequent retry at a higher
                    # layer (new backend instance or not) doesn't stay
                    # permanently tripped.
                    self._consecutive_salvage_misses = 0
                    return NextStepResult(
                        parsed=synthesized,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        reasoning_tokens=reasoning_tokens,
                        reasoning=None,
                        tool_calls=None,
                    )
                content_head = content[:800]
                # Log raw content preview so post-mortem can see what the
                # model sent. ValidationError below is the caller-visible
                # signal; this log is the debug trail. Keep the critique
                # hint at 200 chars — it goes back to the model and we
                # don't want to waste its context window.
                _LOG.warning(
                    "salvage_miss: content-only reply, no JSON object found; "
                    "content[:800]=%r",
                    content_head,
                )
                hint_head = content[:200]
                hint = (
                    "tool_calls missing: you replied with prose instead of "
                    "a tool call. You MUST call exactly one tool per turn "
                    "using the OpenAI tool_calls mechanism (not free text). "
                    f"Your content started with: {hint_head!r}"
                )
                raise ValidationError.from_exception_data(
                    "NextStep",
                    [
                        {
                            "type": "value_error",
                            "loc": ("function",),
                            "input": hint_head,
                            "ctx": {"error": ValueError(hint)},
                        }
                    ],
                )
        else:
            # Native tool_call present — adapter handles the standard
            # extraction path. Distinguish malformed args (native call
            # with broken JSON or schema mismatch) from the no-tool_calls
            # salvage branch above so the circuit breaker only counts
            # actual salvage misses.
            call = tool_calls[0]
            raw_args = call.function.arguments or "{}"
            try:
                _json.loads(raw_args)
            except _json.JSONDecodeError as exc:
                raise ValidationError.from_exception_data(
                    "NextStep",
                    [
                        {
                            "type": "json_invalid",
                            "loc": ("function",),
                            "input": raw_args,
                            "ctx": {"error": str(exc)},
                        }
                    ],
                )
            parsed = self._adapter.extract_next_step(choice.message)
            if parsed is None:
                # Tool_calls were present but the adapter refused — typically
                # schema validation failure on the tool's own parameters.
                # Surface as ValidationError so the agent gets a critique.
                raise ValidationError.from_exception_data(
                    "NextStep",
                    [
                        {
                            "type": "value_error",
                            "loc": ("function",),
                            "input": raw_args[:200],
                            "ctx": {"error": ValueError(
                                f"tool {call.function.name!r} args failed schema validation"
                            )},
                        }
                    ],
                )
            # Successful native tool_call resets the circuit breaker.
            self._consecutive_salvage_misses = 0

        return NextStepResult(
            parsed=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            reasoning=reasoning_text,
            tool_calls=tool_calls_raw,
        )

    def call_structured(
        self,
        prompt: str,
        response_schema: type[_T],
        *,
        timeout_sec: float = 30.0,
    ) -> _T:
        """One-shot structured call for preflight/classifier tools.

        Uses ``beta.chat.completions.parse`` with ``response_format=<schema>``
        — no tools, no tool_choice. LM Studio + gpt-oss-20b supports JSON
        Schema via this path. On parse-returns-None (LM Studio sometimes
        drops ``parsed`` even when content is valid JSON), fall back to
        manual ``model_validate_json`` on the raw content.
        """
        payload = [{"role": "user", "content": prompt}]
        effort = (
            self._adapter.profile.classifier_reasoning_effort
            or self._reasoning_effort
        )
        try:
            with _watchdog_guard(self._adapter, self._model):
                completion = self._client.beta.chat.completions.parse(
                    model=self._model,
                    messages=payload,
                    response_format=response_schema,
                    timeout=timeout_sec,
                    max_tokens=self._adapter.profile.max_completion_tokens,
                    extra_body={"reasoning": {"effort": effort}},
                )
            parsed = completion.choices[0].message.parsed
            if parsed is not None:
                return parsed
            raw = completion.choices[0].message.content or ""
            return response_schema.model_validate_json(raw)
        except (openai.RateLimitError, openai.APITimeoutError,
                openai.APIConnectionError, openai.InternalServerError,
                httpx.ReadTimeout) as exc:
            raise TransientBackendError(str(exc)) from exc
        except openai.BadRequestError as exc:
            msg = str(exc).lower()
            if ("model reloaded" in msg or "model has crashed" in msg
                    or "model unloaded" in msg):
                raise TransientBackendError(str(exc)) from exc
            raise
        except openai.NotFoundError as exc:
            # See the comment on the agent-loop NotFoundError clause
            # above — neuraldeep gateway routing flap reads as transient
            # via the ``page not found`` body marker.
            msg = str(exc).lower()
            if "page not found" in msg:
                raise TransientBackendError(str(exc)) from exc
            raise
