"""Unit tests for OpenAIToolCallingBackend.

Mock-backed — no network. Asserts tool catalog shape, tool_call→NextStep
adaptation, and that transient OpenAI errors are remapped to
``TransientBackendError`` so the agent's P2 retry wrapper kicks in.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from bitgn_contest_agent.backend.base import (
    Message,
    NextStepResult,
    TransientBackendError,
)
from bitgn_contest_agent.backend.openai_toolcalling import (
    OpenAIToolCallingBackend,
    _build_next_step,
    _build_payload,
    _extract_first_json_object,
    _strip_harmony,
    _try_salvage_from_content,
    build_tool_catalog,
)
from bitgn_contest_agent.schemas import NextStep


_ENVELOPE = {
    "current_state": "reading rules",
    "plan_remaining_steps_brief": ["read", "report"],
    "identity_verified": False,
    "observation": "starting a task",
    "outcome_leaning": "GATHERING_INFORMATION",
}


def _envelope_copy() -> dict[str, Any]:
    return dict(_ENVELOPE)


def _mk_completion(*, tool_name: str, arguments: dict[str, Any] | str,
                   prompt_tokens: int = 10, completion_tokens: int = 5,
                   reasoning_tokens: int = 0) -> MagicMock:
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    tc = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = arguments
    msg = MagicMock()
    msg.tool_calls = [tc]
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=MagicMock(reasoning_tokens=reasoning_tokens),
    )
    return completion


def test_tool_catalog_includes_filesystem_and_preflight_tools() -> None:
    cat = build_tool_catalog()
    names = {t["function"]["name"] for t in cat}
    expected = {
        "read", "write", "delete", "mkdir", "move",
        "list", "tree", "find", "search", "context",
        "preflight_schema", "preflight_inbox", "preflight_finance",
        "preflight_entity", "preflight_project", "preflight_doc_migration",
        "report_completion",
    }
    assert names == expected


def test_tool_catalog_every_tool_exposes_envelope_fields_as_properties() -> None:
    """Envelope fields are advertised on every tool so good models fill them,
    but not listed as REQUIRED — small local models ignore required on
    everything except the tool's own fields, and we'd rather default-fill."""
    for t in build_tool_catalog():
        props = t["function"]["parameters"]["properties"]
        required = t["function"]["parameters"]["required"]
        for env in (
            "current_state",
            "plan_remaining_steps_brief",
            "identity_verified",
            "observation",
            "outcome_leaning",
        ):
            assert env in props, f"{t['function']['name']} missing {env} property"
            assert env not in required, \
                f"{t['function']['name']} should not REQUIRE {env} — defaults cover it"


def test_tool_catalog_no_oneof_nodes() -> None:
    """Flat per-tool schemas — no oneOf anywhere. That's the whole point."""
    cat = build_tool_catalog()
    blob = json.dumps(cat)
    assert '"oneOf"' not in blob
    assert '"anyOf"' not in blob


def test_build_next_step_roundtrip_read() -> None:
    args = {**_envelope_copy(), "path": "AGENTS.md"}
    ns = _build_next_step("read", args)
    assert isinstance(ns, NextStep)
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    assert ns.observation == "starting a task"


def test_build_next_step_roundtrip_report_completion() -> None:
    args = {
        **_envelope_copy(),
        "message": "done",
        "grounding_refs": ["AGENTS.md"],
        "rulebook_notes": "ok",
        "outcome_justification": "evidence",
        "completed_steps_laconic": ["read", "report"],
        "outcome": "OUTCOME_OK",
    }
    ns = _build_next_step("report_completion", args)
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_OK"


def test_build_next_step_fills_envelope_defaults_when_missing() -> None:
    """Tool-specific args alone are enough — envelope fields default-fill."""
    ns = _build_next_step("read", {"path": "AGENTS.md"})
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    assert ns.current_state == "(not provided by model)"
    assert ns.observation == "(not provided by model)"
    assert ns.outcome_leaning == "GATHERING_INFORMATION"
    assert ns.plan_remaining_steps_brief == ["continue task"]
    assert ns.identity_verified is False


def test_build_next_step_empty_tool_args_still_raises() -> None:
    """Missing the tool's own required field (path) must still raise."""
    with pytest.raises(ValidationError):
        _build_next_step("read", {})


def test_next_step_happy_path_returns_result_with_tokens() -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mk_completion(
        tool_name="read",
        arguments={**_envelope_copy(), "path": "AGENTS.md"},
        prompt_tokens=7, completion_tokens=11, reasoning_tokens=3,
    )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    out = backend.next_step(
        messages=[Message(role="user", content="t")],
        response_schema=NextStep,
        timeout_sec=30.0,
    )
    assert isinstance(out, NextStepResult)
    assert out.parsed.function.tool == "read"
    assert out.prompt_tokens == 7
    assert out.completion_tokens == 11
    assert out.reasoning_tokens == 3
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs.get("tool_choice") == "required"
    assert kwargs.get("stream") in (None, False)
    assert len(kwargs.get("tools")) == len(build_tool_catalog())
    # LM Studio's nested reasoning-effort convention — without this, the
    # server silently falls back to the GUI-configured effort (observed as
    # <10 reasoning_tokens/turn on the 22/104 PROD baseline).
    assert kwargs.get("extra_body") == {"reasoning": {"effort": "medium"}}


def test_next_step_model_reloaded_400_is_transient() -> None:
    """LM Studio returns 400 'Model reloaded.' to in-flight requests when
    it swaps weights. That blast-radius (one event hitting all parallel
    tasks at once) must be a transient retry, not a hard crash."""
    import openai as _openai
    fake_client = MagicMock()
    fake_response = MagicMock(status_code=400)
    fake_response.json = MagicMock(return_value={"error": "Model reloaded."})
    fake_response.text = '{"error": "Model reloaded."}'
    err = _openai.BadRequestError(
        message="Error code: 400 - {'error': 'Model reloaded.'}",
        response=fake_response,
        body={"error": "Model reloaded."},
    )
    fake_client.chat.completions.create.side_effect = err
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    from bitgn_contest_agent.backend.base import TransientBackendError
    with pytest.raises(TransientBackendError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


def test_next_step_model_crashed_400_is_transient() -> None:
    """LM Studio returns 400 'The model has crashed without additional
    information. (Exit code: null)' when the model slot dies (OOM or
    server-side segfault). Like 'Model reloaded', it hits every in-flight
    request at once — must be reclassified as transient so the retry loop
    waits out the slot restart instead of killing the parallel cohort."""
    import openai as _openai
    fake_client = MagicMock()
    fake_response = MagicMock(status_code=400)
    crash_body = {
        "error": "The model has crashed without additional information. (Exit code: null)"
    }
    fake_response.json = MagicMock(return_value=crash_body)
    fake_response.text = json.dumps(crash_body)
    err = _openai.BadRequestError(
        message=f"Error code: 400 - {crash_body}",
        response=fake_response,
        body=crash_body,
    )
    fake_client.chat.completions.create.side_effect = err
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(TransientBackendError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


def test_next_step_model_unloaded_400_is_transient() -> None:
    """The watchdog (lmstudio_watchdog) force-unloads the model on a
    wall-clock overrun. The in-flight HTTP call then returns 400
    'Model unloaded.' instead of a connection drop — without retry
    classification, the tasks whose runaway the watchdog just freed the
    slot for crash with 'unhandled crash: Error code: 400'. Observed
    2026-04-22 PROD run at v0.1.18: 6/6 watchdog fires → 6 hard task
    losses. Fix is symmetric with the 'Model reloaded.' / 'crashed'
    paths above."""
    import openai as _openai
    fake_client = MagicMock()
    fake_response = MagicMock(status_code=400)
    fake_response.json = MagicMock(return_value={"error": "Model unloaded."})
    fake_response.text = '{"error": "Model unloaded."}'
    err = _openai.BadRequestError(
        message="Error code: 400 - {'error': 'Model unloaded.'}",
        response=fake_response,
        body={"error": "Model unloaded."},
    )
    fake_client.chat.completions.create.side_effect = err
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(TransientBackendError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


def test_call_structured_model_unloaded_400_is_transient() -> None:
    """Same fix at the call_structured (beta.parse) call site — the
    classifier probe is the most frequent watchdog target and must not
    hard-crash the task on an unload."""
    import openai as _openai
    fake_client = MagicMock()
    fake_response = MagicMock(status_code=400)
    fake_response.json = MagicMock(return_value={"error": "Model unloaded."})
    fake_response.text = '{"error": "Model unloaded."}'
    err = _openai.BadRequestError(
        message="Error code: 400 - {'error': 'Model unloaded.'}",
        response=fake_response,
        body={"error": "Model unloaded."},
    )
    fake_client.beta.chat.completions.parse.side_effect = err
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )

    class _Schema:
        @classmethod
        def model_validate_json(cls, raw: str):
            raise AssertionError("should not be reached")

    with pytest.raises(TransientBackendError):
        backend.call_structured("prompt", _Schema, timeout_sec=30.0)


def test_next_step_other_400_still_raises_bad_request() -> None:
    """Other 400s (genuine bad payloads) must surface as BadRequestError,
    not be silently retried."""
    import openai as _openai
    fake_client = MagicMock()
    fake_response = MagicMock(status_code=400)
    fake_response.json = MagicMock(return_value={"error": "bad request"})
    fake_response.text = '{"error": "bad request"}'
    err = _openai.BadRequestError(
        message="Error code: 400 - {'error': 'bad request'}",
        response=fake_response,
        body={"error": "bad request"},
    )
    fake_client.chat.completions.create.side_effect = err
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(_openai.BadRequestError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


def test_next_step_no_tool_calls_is_validation_error() -> None:
    """Content-only replies that cannot be salvaged (no JSON) surface as
    ValidationError so the agent's P3 critique retry kicks in."""
    fake_client = MagicMock()
    msg = MagicMock()
    msg.tool_calls = []
    msg.content = ""
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(prompt_tokens=1, completion_tokens=0,
                                 completion_tokens_details=None)
    fake_client.chat.completions.create.return_value = completion
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(ValidationError) as ei:
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    assert "tool_calls" in str(ei.value)


def test_next_step_malformed_args_is_validation_error() -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mk_completion(
        tool_name="read",
        arguments="not-json{",
    )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


def test_next_step_missing_envelope_fields_defaults_and_succeeds() -> None:
    """Small local models commonly omit envelope fields. Defaults kick in
    so the agent can keep turning — trading observation quality for
    forward progress. Only missing tool-own fields fail."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mk_completion(
        tool_name="read",
        arguments={"path": "AGENTS.md"},
    )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert out.parsed.function.tool == "read"
    assert out.parsed.current_state == "(not provided by model)"


def test_rate_limit_is_remapped_to_transient_backend_error() -> None:
    import openai
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = openai.RateLimitError(
        message="slow down",
        response=MagicMock(status_code=429),
        body=None,
    )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(TransientBackendError):
        backend.next_step([Message(role="user", content="t")], NextStep, 30.0)


def test_timeout_is_remapped_to_transient_backend_error() -> None:
    import openai
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = openai.APITimeoutError(
        request=MagicMock()
    )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(TransientBackendError):
        backend.next_step([Message(role="user", content="t")], NextStep, 30.0)


def test_extract_first_json_object_returns_none_for_empty_string() -> None:
    assert _extract_first_json_object("") is None


def test_extract_first_json_object_returns_none_when_no_braces() -> None:
    assert _extract_first_json_object("plain prose, nothing to parse") is None


def test_extract_first_json_object_parses_bare_object() -> None:
    assert _extract_first_json_object('{"a": 1}') == {"a": 1}


def test_extract_first_json_object_parses_object_wrapped_in_prose() -> None:
    text = 'Sure, here you go:\n{"name": "read", "arguments": {"path": "x"}}\nHope that helps.'
    assert _extract_first_json_object(text) == {
        "name": "read", "arguments": {"path": "x"},
    }


def test_extract_first_json_object_handles_braces_inside_strings() -> None:
    text = '{"s": "has { brace", "n": 1}'
    assert _extract_first_json_object(text) == {"s": "has { brace", "n": 1}


def test_extract_first_json_object_handles_nested_objects() -> None:
    text = '{"outer": {"inner": {"leaf": 1}}}'
    assert _extract_first_json_object(text) == {
        "outer": {"inner": {"leaf": 1}},
    }


def test_extract_first_json_object_skips_broken_first_object_and_finds_next() -> None:
    text = 'garbage {not-json:here} then {"ok": 1}'
    assert _extract_first_json_object(text) == {"ok": 1}


def test_salvage_parses_bare_name_arguments_shape() -> None:
    """lfm2 emits the OpenAI tool shape as free text. Salvage it."""
    content = '{"name": "read", "arguments": {"path": "AGENTS.md"}}'
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"


def test_salvage_rejects_unknown_tool_name() -> None:
    content = '{"name": "rm_minus_rf", "arguments": {"path": "/"}}'
    assert _try_salvage_from_content(content) is None


def test_salvage_returns_none_on_empty_content() -> None:
    assert _try_salvage_from_content("") is None


def test_salvage_returns_none_when_arguments_missing() -> None:
    content = '{"name": "read"}'
    assert _try_salvage_from_content(content) is None


def test_salvage_parses_full_next_step_envelope_shape() -> None:
    """gpt-oss-20b sometimes emits the full envelope as free text."""
    payload = {
        **_envelope_copy(),
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    content = f"Sure thing:\n{json.dumps(payload)}\n"
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    assert ns.current_state == "reading rules"


def test_salvage_returns_none_when_envelope_function_fails_validation() -> None:
    payload = {**_envelope_copy(), "function": {"tool": "read"}}  # no path
    content = json.dumps(payload)
    assert _try_salvage_from_content(content) is None


def test_salvage_returns_none_for_envelope_missing_function_tool() -> None:
    """If function dict has no tool discriminator, salvage returns None."""
    payload = {**_envelope_copy(), "function": {"path": "x"}}
    assert _try_salvage_from_content(json.dumps(payload)) is None


def test_salvage_envelope_with_empty_strings_uses_defaults() -> None:
    """gpt-oss-20b emits envelope JSON with ``current_state=""`` and
    ``observation=""`` — both ``NonEmptyStr``. Salvage must route
    through ``_build_next_step`` so ``_ENVELOPE_DEFAULTS`` papers over
    the empties. Spec §Problem lines 14–17."""
    payload = {
        "current_state": "",
        "plan_remaining_steps_brief": ["read", "report"],
        "identity_verified": False,
        "observation": "",
        "outcome_leaning": "GATHERING_INFORMATION",
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    content = json.dumps(payload)
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    # Defaults kicked in for empty envelope fields.
    assert ns.current_state == "(not provided by model)"
    assert ns.observation == "(not provided by model)"


def test_salvage_prefers_name_arguments_shape_when_both_keys_present() -> None:
    """If content contains {name, arguments, function}, the name/arguments
    branch wins (it's the one small models emit — the envelope key is
    coincidental)."""
    content = json.dumps({
        "name": "read",
        "arguments": {"path": "A.md"},
        "function": {"tool": "write", "path": "B.md", "content": "x"},
    })
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "A.md"


def _mk_content_only_completion(*, content: str,
                                prompt_tokens: int = 4,
                                completion_tokens: int = 2) -> MagicMock:
    msg = MagicMock()
    msg.tool_calls = []
    msg.content = content
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=MagicMock(reasoning_tokens=0),
    )
    return completion


def test_next_step_salvages_content_only_name_arguments_reply() -> None:
    """When tool_calls is empty but content holds a bare {name,arguments}
    object, the backend salvages it into a NextStep instead of raising."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_content_only_completion(
            content='{"name": "read", "arguments": {"path": "AGENTS.md"}}',
        )
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert isinstance(out, NextStepResult)
    assert out.parsed.function.tool == "read"
    assert out.parsed.function.path == "AGENTS.md"
    assert out.prompt_tokens == 4
    assert out.completion_tokens == 2


def test_next_step_raises_validation_error_when_salvage_fails() -> None:
    """Empty content (no JSON to salvage) must still surface ValidationError."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_content_only_completion(content="I don't know what to do.")
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(ValidationError):
        backend.next_step([Message(role="user", content="t")], NextStep, 30.0)


def test_valid_tool_names_matches_built_tool_catalog() -> None:
    """Drift guard: if a new Req_* is added to schemas.py, either
    _VALID_TOOL_NAMES must be updated alongside it or this test fails
    loudly. Protects the salvage allowlist from silent widening."""
    from bitgn_contest_agent.backend.openai_toolcalling import (
        _VALID_TOOL_NAMES,
    )
    catalog_names = {t["function"]["name"] for t in build_tool_catalog()}
    assert catalog_names == _VALID_TOOL_NAMES


def test_salvage_envelope_missing_entirely_uses_defaults() -> None:
    """Bare {"function": {...}} content — no envelope keys at all — must
    default-fill every envelope field via _build_next_step."""
    payload = {"function": {"tool": "read", "path": "x"}}
    ns = _try_salvage_from_content(json.dumps(payload))
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "x"
    assert ns.current_state == "(not provided by model)"
    assert ns.observation == "(not provided by model)"
    assert ns.plan_remaining_steps_brief == ["continue task"]
    assert ns.identity_verified is False
    assert ns.outcome_leaning == "GATHERING_INFORMATION"


def test_salvage_recovers_truncated_envelope_missing_closing_braces() -> None:
    """When LM Studio cuts the response at max_tokens mid-JSON, the envelope
    ends with open braces/brackets (no string-mid truncation). Salvage must
    still recover something usable by appending the missing closers."""
    # Valid envelope with function but closing braces stripped off.
    payload = {
        "current_state": "reading rules",
        "plan_remaining_steps_brief": ["read", "report"],
        "identity_verified": False,
        "observation": "starting",
        "outcome_leaning": "GATHERING_INFORMATION",
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    full = json.dumps(payload)
    # Simulate mid-structure truncation: drop the trailing "}}"
    truncated = full[:-2]
    ns = _try_salvage_from_content(truncated)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"


def test_salvage_recovers_truncated_envelope_mid_string() -> None:
    """Truncation inside a string value: close the string and the object."""
    # Envelope truncated mid-string (inside the last path value).
    truncated = (
        '{"current_state":"reading","plan_remaining_steps_brief":["a"],'
        '"identity_verified":false,"observation":"obs",'
        '"outcome_leaning":"GATHERING_INFORMATION",'
        '"function":{"tool":"read","path":"02_distill/cards/very-long-file-na'
    )
    ns = _try_salvage_from_content(truncated)
    assert ns is not None
    assert ns.function.tool == "read"
    # Path is whatever we could recover before the cut.
    assert ns.function.path.startswith("02_distill/cards/")


def test_extract_first_json_object_repairs_simple_truncation() -> None:
    """Direct _extract_first_json_object check: truncated input still yields
    a parseable dict via the repair pass."""
    truncated = '{"a": 1, "b": [1, 2'
    obj = _extract_first_json_object(truncated)
    assert obj is not None
    assert obj["a"] == 1
    assert obj["b"] == [1, 2]


def test_build_next_step_caps_plan_remaining_steps_brief_at_5() -> None:
    """Real repro: gpt-oss-20b emits a valid envelope with 9 delete plan
    items, which violates maxItems=5 and fails NextStep validation.
    _build_next_step must truncate to the first 5 items so the step
    goes through instead of cascading to double-validation failure."""
    from bitgn_contest_agent.backend.openai_toolcalling import _build_next_step
    args = {
        "current_state": "ready to delete",
        "plan_remaining_steps_brief": [
            "delete a.md", "delete b.md", "delete c.md",
            "delete d.md", "delete e.md", "delete f.md",
            "delete g.md", "delete h.md", "delete i.md",
        ],
        "identity_verified": True,
        "observation": "Identity verified",
        "outcome_leaning": "GATHERING_INFORMATION",
        "path": "a.md",
    }
    ns = _build_next_step("delete", args)
    assert len(ns.plan_remaining_steps_brief) == 5
    assert ns.plan_remaining_steps_brief[0] == "delete a.md"
    assert ns.function.tool == "delete"
    assert ns.function.path == "a.md"


def test_build_next_step_normalizes_invalid_outcome_leaning() -> None:
    """If the model emits a string not in the enum, fall back to
    GATHERING_INFORMATION instead of failing validation."""
    from bitgn_contest_agent.backend.openai_toolcalling import _build_next_step
    args = {
        "current_state": "s",
        "plan_remaining_steps_brief": ["step"],
        "identity_verified": False,
        "observation": "o",
        "outcome_leaning": "OUTCOME_MAYBE_OK",  # not in enum
        "path": "x",
    }
    ns = _build_next_step("read", args)
    assert ns.outcome_leaning == "GATHERING_INFORMATION"


def test_salvage_envelope_with_9_plan_items_succeeds() -> None:
    """End-to-end salvage path: envelope-shape content with over-long
    plan list must be recovered (not returned as None) by virtue of the
    cap applied inside _build_next_step."""
    import json as _json
    content = _json.dumps({
        "current_state": "ready",
        "plan_remaining_steps_brief": [f"delete {i}" for i in range(9)],
        "identity_verified": True,
        "observation": "ok",
        "outcome_leaning": "GATHERING_INFORMATION",
        "function": {"tool": "delete", "path": "a.md"},
    })
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert len(ns.plan_remaining_steps_brief) == 5
    assert ns.function.path == "a.md"


def test_next_step_sets_max_tokens_on_completion_call() -> None:
    """Guard: backend MUST pass a non-trivial max_tokens to the server so
    LM Studio's default cap does not truncate a long envelope reply."""
    fake = MagicMock()
    fake.chat.completions.create.return_value = _mk_completion(
        tool_name="read",
        arguments={**_envelope_copy(), "path": "AGENTS.md"},
    )
    backend = OpenAIToolCallingBackend(
        client=fake, model="m", reasoning_effort="medium",
    )
    backend.next_step([Message(role="user", content="t")], NextStep, 30.0)
    _, kwargs = fake.chat.completions.create.call_args
    assert "max_tokens" in kwargs
    assert kwargs["max_tokens"] >= 2048


# --- gpt-oss harmony stripper tests --------------------------------------
#
# LM Studio's chat template parser for openai/gpt-oss-20b occasionally
# routes "harmony" channel markers into the content field instead of into
# tool_calls / reasoning_content. Four shapes were observed in v9-v11 PROD
# logs; salvage must recover all four without regressing the existing
# bare-JSON shapes.


def test_strip_harmony_returns_content_unchanged_when_no_header() -> None:
    body = '{"name": "read", "arguments": {"path": "x"}}'
    tool, stripped = _strip_harmony(body)
    assert tool is None
    assert stripped == body


def test_strip_harmony_captures_tool_from_commentary_header() -> None:
    content = (
        '<|channel|>commentary to=functions.read '
        '<|constrain|>json<|message|>{"path": "AGENTS.md"}<|call|>'
    )
    tool, stripped = _strip_harmony(content)
    assert tool == "read"
    assert stripped == '{"path": "AGENTS.md"}'


def test_strip_harmony_strips_final_channel_without_tool() -> None:
    content = (
        '<|channel|>final <|constrain|>json<|message|>'
        '{"current_state": "ok"}<|end|>'
    )
    tool, stripped = _strip_harmony(content)
    assert tool is None
    assert stripped == '{"current_state": "ok"}'


def test_strip_harmony_strips_return_and_end_sentinels() -> None:
    content = (
        '<|channel|>final<|message|>{"a": 1}<|return|>'
    )
    tool, stripped = _strip_harmony(content)
    assert tool is None
    assert stripped == '{"a": 1}'


def test_salvage_commentary_harmony_with_bare_arguments() -> None:
    """Complete harmony commentary shape: body is bare arguments for the
    target tool; envelope defaults must fill in the NextStep envelope."""
    content = (
        '<|channel|>commentary to=functions.read '
        '<|constrain|>json<|message|>'
        '{"path": "AGENTS.md"}<|call|>'
    )
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    # Envelope defaults (not supplied in the harmony body) must be applied.
    assert ns.current_state == "(not provided by model)"


def test_salvage_final_harmony_with_full_envelope() -> None:
    """Complete harmony final shape: body is a full NextStep envelope —
    salvage must parse it via the existing shape-3 path."""
    inner = json.dumps({
        "current_state": "ready",
        "plan_remaining_steps_brief": ["read"],
        "identity_verified": True,
        "observation": "ok",
        "outcome_leaning": "GATHERING_INFORMATION",
        "function": {"tool": "read", "path": "AGENTS.md"},
    })
    content = (
        f'<|channel|>final <|constrain|>json<|message|>{inner}<|end|>'
    )
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"
    assert ns.current_state == "ready"
    assert ns.identity_verified is True


def test_salvage_truncated_commentary_harmony() -> None:
    """Harmony commentary header with truncated JSON body (max_tokens cut).
    Expect the repair pass to close the missing braces and recover."""
    content = (
        '<|channel|>commentary to=functions.read '
        '<|constrain|>json<|message|>'
        '{"path": "02_distill/cards/very-long-file-'
    )
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path.startswith("02_distill/cards/")


def test_salvage_truncated_final_harmony() -> None:
    """Harmony final header with truncated envelope body; shape-3 salvage
    via repair pass must still produce a valid NextStep."""
    partial_inner = (
        '{"current_state":"reading","plan_remaining_steps_brief":["a"],'
        '"identity_verified":false,"observation":"o",'
        '"outcome_leaning":"GATHERING_INFORMATION",'
        '"function":{"tool":"read","path":"AGENTS.md"'
    )
    content = (
        f'<|channel|>final <|constrain|>json<|message|>{partial_inner}'
    )
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"


def test_strip_harmony_captures_tool_via_final_nested_commentary() -> None:
    """v12 shape: ``<|channel|>final <|constrain|>commentary to=functions.X``
    — the tool target appears after the channel name but inside a nested
    ``<|constrain|>`` block. Stripper must still extract the tool."""
    content = (
        '<|channel|>final <|constrain|>commentary '
        'to=functions.preflight_doc_migration <|constrain|>json<|message|>'
        '{"entities_root": "01_entity"}<|end|>'
    )
    tool, stripped = _strip_harmony(content)
    assert tool == "preflight_doc_migration"
    assert stripped == '{"entities_root": "01_entity"}'


def test_strip_harmony_captures_tool_via_bare_constrain() -> None:
    """v12 shape: ``<|channel|>final <|constrain|>report_completion<|message|>``
    — no ``to=functions.`` prefix; tool name is the sole word inside
    ``<|constrain|>``. Must match because report_completion is valid."""
    content = (
        '<|channel|>final <|constrain|>report_completion<|message|>'
        '{"outcome": "OUTCOME_OK"}<|end|>'
    )
    tool, stripped = _strip_harmony(content)
    assert tool == "report_completion"
    assert stripped == '{"outcome": "OUTCOME_OK"}'


def test_strip_harmony_bare_constrain_ignores_json_marker() -> None:
    """Guard: ``<|constrain|>json<|message|>`` must NOT be treated as a tool
    name — json isn't a valid tool. The stripper falls through to the
    generic final-header match and returns no tool."""
    content = (
        '<|channel|>final <|constrain|>json<|message|>'
        '{"current_state": "x"}<|end|>'
    )
    tool, stripped = _strip_harmony(content)
    assert tool is None
    assert stripped == '{"current_state": "x"}'


def test_salvage_truncated_final_nested_commentary_harmony() -> None:
    """v12 shape end-to-end: nested-commentary header with truncated body
    that still holds enough tool-specific args to validate. Tool must be
    captured from the header and the body repaired via closers."""
    content = (
        '<|channel|>final <|constrain|>commentary '
        'to=functions.read <|constrain|>json<|message|>'
        '{"path": "01_entity/companies/ACME.md"'
    )
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "01_entity/companies/ACME.md"


def test_salvage_harmony_analysis_channel_then_commentary() -> None:
    """LM Studio sometimes emits an analysis channel as prelude before the
    commentary tool call. _strip_harmony matches the commentary header
    (the first header with a tool target), dropping the analysis prose."""
    content = (
        '<|channel|>analysis<|message|>Let me think about this.<|end|>'
        '<|channel|>commentary to=functions.read '
        '<|constrain|>json<|message|>'
        '{"path": "AGENTS.md"}<|call|>'
    )
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "read"
    assert ns.function.path == "AGENTS.md"


# --- envelope-only terminal salvage --------------------------------------
#
# gpt-oss-20b / lfm2 occasionally emit the NextStep envelope as free text
# with ``outcome_leaning`` set to a terminal non-OK value but NO ``function``
# or ``name`` key. Without salvage, the agent critiques, retries, and often
# burns through its turn budget ending in ``OUTCOME_ERR_INTERNAL``. Safer:
# synthesize the ``report_completion`` the model already telegraphed. Only
# non-OK terminals — claiming OK without a committed answer would fabricate
# a pass.


def test_salvage_envelope_only_none_unsupported_synthesizes_report() -> None:
    content = json.dumps({
        "current_state": "No inbox_root available, cannot proceed",
        "plan_remaining_steps_brief": ["give up"],
        "identity_verified": False,
        "observation": "entities_root missing",
        "outcome_leaning": "OUTCOME_NONE_UNSUPPORTED",
    })
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_NONE_UNSUPPORTED"
    assert "inbox_root" in ns.function.message


def test_salvage_envelope_only_none_clarification_synthesizes_report() -> None:
    content = json.dumps({
        "current_state": "ambiguous request",
        "plan_remaining_steps_brief": ["ask"],
        "identity_verified": False,
        "observation": "need clarification",
        "outcome_leaning": "OUTCOME_NONE_CLARIFICATION",
    })
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_NONE_CLARIFICATION"


def test_salvage_envelope_only_denied_security_synthesizes_report() -> None:
    content = json.dumps({
        "current_state": "request blocked by policy",
        "plan_remaining_steps_brief": ["refuse"],
        "identity_verified": True,
        "observation": "security filter triggered",
        "outcome_leaning": "OUTCOME_DENIED_SECURITY",
    })
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_DENIED_SECURITY"


def test_salvage_envelope_only_terminal_via_harmony_header() -> None:
    inner = json.dumps({
        "current_state": "No entities root available",
        "plan_remaining_steps_brief": [],
        "identity_verified": False,
        "observation": "cannot proceed",
        "outcome_leaning": "OUTCOME_NONE_UNSUPPORTED",
    })
    content = f'<|channel|>final <|constrain|>json<|message|>{inner}<|end|>'
    ns = _try_salvage_from_content(content)
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_NONE_UNSUPPORTED"


def test_salvage_envelope_only_gathering_info_returns_none() -> None:
    """Non-terminal leaning — no synthesis; let the critique path run."""
    content = json.dumps({
        "current_state": "still figuring out",
        "plan_remaining_steps_brief": ["think more"],
        "identity_verified": False,
        "observation": "ambiguous",
        "outcome_leaning": "GATHERING_INFORMATION",
    })
    assert _try_salvage_from_content(content) is None


def test_salvage_envelope_only_outcome_ok_returns_none() -> None:
    """OUTCOME_OK without a committed answer — synthesizing here would
    fabricate a pass. Skip so the critique path runs."""
    content = json.dumps({
        "current_state": "done",
        "plan_remaining_steps_brief": ["done"],
        "identity_verified": True,
        "observation": "finished",
        "outcome_leaning": "OUTCOME_OK",
    })
    assert _try_salvage_from_content(content) is None


def test_salvage_envelope_with_function_still_prefers_function_branch() -> None:
    """Guard: when both function and terminal leaning are present, the
    function-shape branch must win — terminal synthesis is the fallback."""
    payload = {
        **_envelope_copy(),
        "outcome_leaning": "OUTCOME_NONE_UNSUPPORTED",
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    ns = _try_salvage_from_content(json.dumps(payload))
    assert ns is not None
    assert ns.function.tool == "read"


# --- CoT-preservation payload builder + NextStepResult capture -----------
#
# Spec 2026-04-16-gpt-oss-cot-preservation-design §Architecture: the
# toolcalling backend must (a) emit the canonical OpenAI assistant-with-
# tool_calls wire shape so LM Studio renders the prior CoT back into the
# gpt-oss chat template, and (b) surface ``reasoning`` + structured
# ``tool_calls`` on NextStepResult so the agent loop can replay them on
# the next turn.


def test_payload_builder_emits_canonical_assistant_tool_call_shape() -> None:
    tool_calls = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "read", "arguments": '{"path":"x"}'},
        }
    ]
    msgs = [
        Message(
            role="assistant",
            content=None,
            tool_calls=tool_calls,
            reasoning="cot-text",
        )
    ]
    payload = _build_payload(msgs)
    assert payload == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
            "reasoning": "cot-text",
        }
    ]


def test_payload_builder_omits_reasoning_when_none() -> None:
    tool_calls = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }
    ]
    msgs = [
        Message(
            role="assistant",
            content=None,
            tool_calls=tool_calls,
            reasoning=None,
        )
    ]
    payload = _build_payload(msgs)
    assert payload == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
        }
    ]
    assert "reasoning" not in payload[0]


def test_payload_builder_emits_tool_role_with_call_id() -> None:
    msgs = [Message(role="tool", content="result", tool_call_id="abc")]
    payload = _build_payload(msgs)
    assert payload == [
        {"role": "tool", "tool_call_id": "abc", "content": "result"}
    ]


def test_payload_builder_falls_back_on_salvage_path() -> None:
    """Salvage path records assistant turn as ``content=<json>`` with no
    ``tool_calls`` — the payload builder must drop into the else branch
    rather than emit an assistant-with-tool_calls shape."""
    msgs = [Message(role="assistant", content='{"a":1}')]
    payload = _build_payload(msgs)
    assert payload == [{"role": "assistant", "content": '{"a":1}'}]


def test_next_step_captures_reasoning_and_tool_calls_into_result() -> None:
    """Mock a completion with ``message.reasoning`` and one tool_call —
    NextStepResult.reasoning must carry the CoT and .tool_calls must hold
    the structured tool_call list for the agent loop to replay."""
    tc = MagicMock()
    tc.function.name = "read"
    tc.function.arguments = json.dumps({**_envelope_copy(), "path": "AGENTS.md"})
    tc.model_dump = MagicMock(return_value={
        "id": "call_xyz",
        "type": "function",
        "function": {"name": "read", "arguments": tc.function.arguments},
    })
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.reasoning = "thinking about reading AGENTS.md"
    msg.content = None
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=3, completion_tokens=4,
        completion_tokens_details=MagicMock(reasoning_tokens=1),
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = completion

    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert isinstance(out, NextStepResult)
    assert out.reasoning == "thinking about reading AGENTS.md"
    assert out.tool_calls is not None
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0]["id"] == "call_xyz"
    assert out.tool_calls[0]["function"]["name"] == "read"


def test_next_step_returns_none_reasoning_when_absent() -> None:
    """When ``choice.message`` has no ``reasoning`` attribute (pre-0.3.23
    LM Studio, or any non-reasoning model), NextStepResult.reasoning must
    surface as ``None`` — not a falsy mock, not an empty string."""
    # Use ``spec`` to lock the attribute namespace; without this a bare
    # MagicMock would auto-fabricate ``reasoning`` as a child MagicMock.
    tc = MagicMock()
    tc.function.name = "read"
    tc.function.arguments = json.dumps({**_envelope_copy(), "path": "AGENTS.md"})
    tc.model_dump = MagicMock(return_value={
        "id": "call_xyz",
        "type": "function",
        "function": {"name": "read", "arguments": tc.function.arguments},
    })
    msg = MagicMock(spec=["tool_calls", "content", "model_dump"])
    msg.tool_calls = [tc]
    msg.content = None
    msg.model_dump = MagicMock(return_value={})  # no 'reasoning' key
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=1, completion_tokens=1,
        completion_tokens_details=MagicMock(reasoning_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = completion

    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert out.reasoning is None
    # tool_calls are still structured (we had one native tool_call).
    assert out.tool_calls is not None
    assert out.tool_calls[0]["id"] == "call_xyz"


# --- salvage_miss log preview + circuit breaker --------------------------
#
# Items 1 and 2: when the model gets stuck in a content-only envelope
# loop, the log preview must capture 800 chars (was 200) for debugging,
# and after N consecutive salvage_miss events on the same backend
# instance we synthesize a terminal ``report_completion`` instead of
# burning the full step budget on critique/retry.


def test_salvage_miss_log_preview_at_800_chars(caplog) -> None:
    """The post-mortem log preview must capture 800 chars of content,
    up from the former 200, so we can see what the model emitted
    without truncation when nothing is salvageable."""
    import logging as _logging
    long_prose = "x" * 900  # non-JSON, >800 chars
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_content_only_completion(content=long_prose)
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with caplog.at_level(
        _logging.WARNING,
        logger="bitgn_contest_agent.backend.openai_toolcalling",
    ):
        with pytest.raises(ValidationError):
            backend.next_step(
                [Message(role="user", content="t")], NextStep, 30.0,
            )
    salvage_records = [r for r in caplog.records if "salvage_miss" in r.message]
    assert salvage_records, "expected a salvage_miss warning to be emitted"
    record = salvage_records[0]
    # The first positional arg is the content preview.
    content_head = record.args[0]
    assert isinstance(content_head, str)
    assert len(content_head) == 800
    assert "content[:800]" in record.message


def test_circuit_breaker_fires_after_three_consecutive_misses() -> None:
    """Three consecutive salvage_miss events on the same backend must
    synthesize a terminal OUTCOME_NONE_UNSUPPORTED report_completion on
    the third turn rather than raising."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = \
        _mk_content_only_completion(content="I don't know what to do.")
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    # First miss → ValidationError.
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    # Second miss → ValidationError.
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    # Third miss → synthesized terminal.
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert isinstance(out, NextStepResult)
    assert out.parsed.function.tool == "report_completion"
    assert out.parsed.function.outcome == "OUTCOME_NONE_UNSUPPORTED"
    assert out.parsed.outcome_leaning == "OUTCOME_NONE_UNSUPPORTED"
    assert out.tool_calls is None


def test_circuit_breaker_resets_on_successful_salvage() -> None:
    """miss → miss → successful salvage → miss must NOT fire the
    breaker: the successful parse resets the counter."""
    fake_client = MagicMock()
    unsalvageable = _mk_content_only_completion(
        content="I don't know what to do.",
    )
    salvageable = _mk_content_only_completion(
        content='{"name": "read", "arguments": {"path": "AGENTS.md"}}',
    )
    fake_client.chat.completions.create.side_effect = [
        unsalvageable,
        unsalvageable,
        salvageable,
        unsalvageable,
    ]
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    # Two misses.
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    # Successful salvage resets the counter.
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert out.parsed.function.tool == "read"
    # A subsequent miss must raise (fresh count=1), NOT synthesize.
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


def test_circuit_breaker_resets_on_successful_tool_call() -> None:
    """miss → miss → native tool_call → miss → miss must NOT fire the
    breaker on the final miss: successful native tool_calls also reset
    the counter."""
    fake_client = MagicMock()
    unsalvageable = _mk_content_only_completion(
        content="I don't know what to do.",
    )
    native_tool_call = _mk_completion(
        tool_name="read",
        arguments={**_envelope_copy(), "path": "AGENTS.md"},
    )
    fake_client.chat.completions.create.side_effect = [
        unsalvageable,
        unsalvageable,
        native_tool_call,
        unsalvageable,
        unsalvageable,
    ]
    backend = OpenAIToolCallingBackend(
        client=fake_client, model="local-model", reasoning_effort="medium",
    )
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    # Native tool_call resets.
    out = backend.next_step(
        [Message(role="user", content="t")], NextStep, 30.0,
    )
    assert out.parsed.function.tool == "read"
    # Two fresh misses — neither trips the breaker.
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )
    with pytest.raises(ValidationError):
        backend.next_step(
            [Message(role="user", content="t")], NextStep, 30.0,
        )


# --- harmony v13 <|constrain|>function: {"tool": ...} shape --------------
#
# Item 3: a new harmony shape seen in PROD task t009 where the tool name
# lives inside a JSON preamble after ``<|constrain|>function:``.


def test_harmony_strips_constrain_function_shape() -> None:
    """v13 harmony shape: tool name is embedded in a JSON preamble after
    ``<|constrain|>function:``. Stripper must capture the tool and peel
    off the header/preamble so the body is the real arguments JSON."""
    content = (
        '<|channel|>final<|constrain|>function: '
        '{"tool": "write", "path": "/tmp/x"}<|message|>'
        '{"content":"hi","path":"/tmp/x"}<|end|>'
    )
    tool, body = _strip_harmony(content)
    assert tool == "write"
    assert body == '{"content":"hi","path":"/tmp/x"}'


def test_harmony_constrain_function_rejects_unknown_tool() -> None:
    """Unknown tool name in the preamble must NOT match the new regex —
    fall through to the bare-constrain / final-header path, yielding
    tool=None and a body that doesn't include the harmony header."""
    content = (
        '<|channel|>final<|constrain|>function: '
        '{"tool": "notarealtool", "path": "/tmp/x"}<|message|>'
        '{"content":"hi"}<|end|>'
    )
    tool, body = _strip_harmony(content)
    assert tool is None
    # Body must not still carry the <|message|> sentinel or the preamble.
    assert "<|message|>" not in body
    assert "<|constrain|>" not in body
    assert body == '{"content":"hi"}'


# ── bare-value salvage ─────────────────────────────────────────────────


def test_salvage_bare_numeric_value() -> None:
    """Model emits '780' as a raw answer — synthesize report_completion."""
    ns = _try_salvage_from_content("780")
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert "780" in ns.function.message


def test_salvage_bare_name_value() -> None:
    ns = _try_salvage_from_content("Tobias")
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert "Tobias" in ns.function.message


def test_salvage_bare_date_value() -> None:
    ns = _try_salvage_from_content("08/16/2019")
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert "08/16/2019" in ns.function.message


def test_salvage_does_not_fire_on_long_prose() -> None:
    """Long prose is not a bare value — let critique handle it."""
    ns = _try_salvage_from_content("I'm not sure what to do here. " * 20)
    assert ns is None


def test_salvage_bare_value_not_triggered_when_json_present() -> None:
    """Content with braces should go through the JSON path, not bare-value."""
    content = '{"name": "read", "arguments": {"path": "x.md"}}'
    ns = _try_salvage_from_content(content)
    assert ns is not None
    # Should be salvaged as a read tool call, not report_completion
    assert ns.function.tool == "read"
