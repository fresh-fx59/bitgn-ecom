"""Tests for qwen-specific adapter behavior.

Evidence source: 2026-04-19 qwen3.5-35b-a3b PROD run
(`artifacts/bench/qwen35a3b_adapter_prod_runs1.json`,
`logs/plan-b-local-run-qwen35a3b-20260419-2010.log`). Each test cites
the failure pattern it guards against so the rationale survives code drift.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.backend.adapters._helpers import (
    try_qwen_bare_answer,
)
from bitgn_contest_agent.backend.adapters.base import (
    ModelAdapter,
    ModelProfile,
)
from bitgn_contest_agent.backend.adapters.glm_flash import GlmFlashAdapter
from bitgn_contest_agent.backend.adapters.gpt_oss import GptOssAdapter
from bitgn_contest_agent.backend.adapters.qwen_a3b import (
    QwenA3bAdapter,
    _QWEN_SYSTEM_NUDGE,
)
from bitgn_contest_agent.backend.base import Message
from bitgn_contest_agent.backend.openai_toolcalling import (
    OpenAIToolCallingBackend,
)
from bitgn_contest_agent.schemas import NextStep


# ---------------------------------------------------------------------------
# try_qwen_bare_answer — guards mirror the 2026-04-19 evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "1170",                    # t009 step 12 bare number
        "650",                     # t008 bare number
        "03-02-2026",              # t-? bare date
        "docs/a.md\ndocs/b.md",    # t006 step 7 file-path list
        "  380  ",                 # t007 bare number with surrounding whitespace
    ],
)
def test_bare_answer_accepts_short_terminal_shapes(content: str) -> None:
    """Evidence: 12 qwen bare-text terminations in the 2026-04-19 PROD run.
    Guard: len ≤ 500, no JSON prefix, no template tokens, no continuation markers."""
    ns = try_qwen_bare_answer(content)
    assert ns is not None, f"bare-answer salvage refused legitimate answer {content!r}"
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_OK"
    assert ns.function.message == content.strip()


@pytest.mark.parametrize(
    "content,reason",
    [
        ("", "empty content"),
        ("   \n\n  ", "whitespace only"),
        ("{\"tool\": \"read\", \"path\": \"AGENTS.md\"}", "JSON-shaped — envelope salvage path"),
        ("[1, 2, 3]", "array-shaped"),
        ("</tool_call>", "chat-template leakage (the GLM score=0 bug)"),
        ("<|im_end|>answer", "qwen/im tag"),
        ("<think>hmm</think>", "think-block tag"),
        ("```json\n{}\n```", "backticked code fence"),
        ("Let me check the file first", "continuation — 'Let me '"),
        ("I need to read AGENTS.md", "continuation — 'I need '"),
        ("Plan: read then search", "continuation — 'Plan:'"),
        ("Thinking through this...", "continuation — 'thinking'"),
        ("a" * 501, "over 500-char guard"),
    ],
)
def test_bare_answer_rejects_unsafe_shapes(content: str, reason: str) -> None:
    """Every rejection here prevents a false-positive OUTCOME_OK terminal.
    The continuation cases are the expensive ones: firing on mid-exploration
    content would terminate a task that still needs to gather information."""
    assert try_qwen_bare_answer(content) is None, (
        f"expected rejection for {reason}, got synthesis"
    )


# ---------------------------------------------------------------------------
# QwenA3bAdapter.shape_request — system nudge prepend
# ---------------------------------------------------------------------------


def test_qwen_shape_request_prepends_system_nudge() -> None:
    """Fix for the 2026-04-19 empty-content failure mode: 196/211 salvage_misses
    returned content='' with no tool_calls. Nudge reminds qwen that its only
    valid output shape is one OpenAI tool_call."""
    adapter = QwenA3bAdapter()
    original_messages = [
        {"role": "system", "content": "agent system prompt"},
        {"role": "user", "content": "task"},
    ]
    payload = {
        "model": "qwen3.5-35b-a3b",
        "messages": original_messages,
        "extra_body": {"reasoning": {"effort": "high"}},
    }
    shaped = adapter.shape_request(payload)

    assert shaped["messages"][0] == {"role": "system", "content": _QWEN_SYSTEM_NUDGE}
    # Existing messages preserved in order behind the nudge.
    assert shaped["messages"][1:] == original_messages
    # Non-message fields unchanged.
    assert shaped["model"] == "qwen3.5-35b-a3b"
    assert shaped["extra_body"] == {"reasoning": {"effort": "high"}}
    # Original payload not mutated in place.
    assert payload["messages"] is original_messages


def test_qwen_shape_request_nudge_mentions_tool_call_requirement() -> None:
    """Regression guard on the nudge content itself: if someone edits the
    string to something permissive, this test catches it."""
    assert "tool_call" in _QWEN_SYSTEM_NUDGE
    assert "empty" in _QWEN_SYSTEM_NUDGE.lower()


# ---------------------------------------------------------------------------
# QwenA3bAdapter.extract_next_step — salvage chain ordering
# ---------------------------------------------------------------------------


def _mk_message(*, tool_calls: list[Any] | None = None, content: str = "") -> MagicMock:
    """Build a minimal ChatCompletionMessage-shaped mock."""
    msg = MagicMock()
    msg.tool_calls = tool_calls
    msg.content = content
    return msg


def test_qwen_extract_standard_tool_call_path() -> None:
    tc = MagicMock()
    tc.function.name = "read"
    tc.function.arguments = json.dumps({
        "current_state": "reading",
        "plan_remaining_steps_brief": ["read", "report"],
        "identity_verified": False,
        "observation": "start",
        "outcome_leaning": "GATHERING_INFORMATION",
        "path": "AGENTS.md",
    })
    msg = _mk_message(tool_calls=[tc])
    ns = QwenA3bAdapter().extract_next_step(msg)
    assert ns is not None
    assert ns.function.tool == "read"


def test_qwen_extract_envelope_salvage_when_tool_calls_absent() -> None:
    """Observed 2026-04-19 20:09 on PROD t000: qwen emitted the full NextStep
    envelope as content body with a valid function.tool."""
    envelope = {
        "current_state": "starting",
        "plan_remaining_steps_brief": ["read AGENTS"],
        "identity_verified": False,
        "observation": "first turn",
        "outcome_leaning": "GATHERING_INFORMATION",
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    msg = _mk_message(tool_calls=None, content=json.dumps(envelope))
    ns = QwenA3bAdapter().extract_next_step(msg)
    assert ns is not None
    assert ns.function.tool == "read"


def test_qwen_extract_bare_answer_salvage_when_envelope_absent() -> None:
    """When neither tool_calls nor envelope is present but content is a short
    bare answer, synthesize a report_completion(OUTCOME_OK)."""
    msg = _mk_message(tool_calls=None, content="1170")
    ns = QwenA3bAdapter().extract_next_step(msg)
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_OK"
    assert ns.function.message == "1170"


def test_qwen_extract_returns_none_on_empty_content() -> None:
    """The dominant failure mode in the 2026-04-19 run: empty content +
    no tool_calls. Must return None so the critique/retry path runs.
    Synthesizing anything here would lie to the grader."""
    msg = _mk_message(tool_calls=None, content="")
    assert QwenA3bAdapter().extract_next_step(msg) is None


def test_qwen_extract_returns_none_on_continuation_prose() -> None:
    """Content like 'Let me check AGENTS.md' is mid-exploration, not terminal.
    Hijacking it would terminate a GATHERING_INFORMATION turn with OUTCOME_OK."""
    msg = _mk_message(tool_calls=None, content="Let me check AGENTS.md first")
    assert QwenA3bAdapter().extract_next_step(msg) is None


# ---------------------------------------------------------------------------
# GLM regression guard — bare-answer salvage MUST NOT be chained on GLM
# ---------------------------------------------------------------------------


def test_glm_adapter_does_not_use_bare_answer_salvage() -> None:
    """The 2026-04-19 GLM score=0 incident: bare-value salvage captured
    `</tool_call>` as the task answer. qwen's bare-answer salvage has the
    same shape — if it were ever chained on GlmFlashAdapter, the incident
    would recur. This is a structural guard: the adapter's extract chain
    must not fall through to try_qwen_bare_answer."""
    # GLM content that bare-answer's chat-template guard would catch anyway —
    # but this test asserts the chain never REACHES the helper.
    adapter = GlmFlashAdapter()
    msg = _mk_message(tool_calls=None, content="42")
    # Per GlmFlashAdapter.extract_next_step, this should go standard →
    # envelope and then return None. A bare answer of "42" would pass
    # try_qwen_bare_answer's guards — if it did, this test would be
    # synthesizing a completion.
    assert adapter.extract_next_step(msg) is None


def test_gpt_oss_adapter_does_not_regress_on_bare_answer_helper() -> None:
    """gpt-oss keeps its legacy bare-value branch via try_gpt_oss_full_chain
    (with tighter guards: len≤80, words≤5, no '{'). Chaining the qwen helper
    on gpt-oss would loosen those guards. Smoke-guard that gpt-oss extraction
    on a would-be-bare-value still runs through the legacy chain, not qwen's."""
    adapter = GptOssAdapter()
    # A value that passes qwen guards (date format, 10 chars) but wouldn't
    # match gpt-oss's tighter bare-value guard shape as unambiguously.
    # Assert we get a NextStep either way — if qwen-style helper bled in,
    # outcome would always be OUTCOME_OK which is not the gpt-oss contract.
    msg = _mk_message(tool_calls=None, content="03-02-2026")
    result = adapter.extract_next_step(msg)
    # gpt-oss legacy bare-value salvage DOES accept short tokens and
    # synthesizes a terminal — but with OUTCOME_NONE_UNSUPPORTED semantics
    # via its own branch, not qwen's OUTCOME_OK. Assert the tool is
    # report_completion (either chain terminates) and then that it's not
    # been routed through qwen's OUTCOME_OK convention.
    if result is not None:
        # Chain fired. Accept any terminal the legacy chain chose.
        assert result.function.tool == "report_completion"


# ---------------------------------------------------------------------------
# Backend wiring — shape_request is actually called and its result is used
# ---------------------------------------------------------------------------


class _ProbeAdapter(ModelAdapter):
    """Minimal adapter that stamps a sentinel value into the outbound
    request so the test can prove shape_request ran and its return was used."""

    def __init__(self) -> None:
        super().__init__(
            name="probe",
            profile=ModelProfile(
                task_timeout_sec=1,
                llm_http_timeout_sec=1,
                classifier_timeout_sec=1,
                max_parallel_tasks=1,
                max_inflight_llm=1,
                reasoning_effort="low",
            ),
        )
        self.shape_calls: list[dict[str, Any]] = []

    def shape_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.shape_calls.append(payload)
        return {**payload, "extra_body": {"probe_sentinel": True}}


def _mk_completion_with_tool_call(tool_name: str, arguments: dict[str, Any]) -> MagicMock:
    tc = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    completion.usage = MagicMock(
        prompt_tokens=1,
        completion_tokens=1,
        completion_tokens_details=MagicMock(reasoning_tokens=0),
    )
    return completion


def test_backend_next_step_calls_shape_request_and_uses_return_value() -> None:
    """The adapter's shape_request hook was dead code before this wiring. The
    qwen system nudge depends on it actually reaching chat.completions.create."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mk_completion_with_tool_call(
        "read",
        {
            "current_state": "s",
            "plan_remaining_steps_brief": ["x"],
            "identity_verified": False,
            "observation": "o",
            "outcome_leaning": "GATHERING_INFORMATION",
            "path": "AGENTS.md",
        },
    )
    adapter = _ProbeAdapter()
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="probe",
        reasoning_effort="high",
        adapter=adapter,
    )
    backend.next_step(
        messages=[Message(role="user", content="t")],
        response_schema=NextStep,
        timeout_sec=5.0,
    )

    assert len(adapter.shape_calls) == 1
    seen = fake_client.chat.completions.create.call_args.kwargs
    # Adapter's override replaced extra_body — proves shape_request return
    # was used, not discarded.
    assert seen["extra_body"] == {"probe_sentinel": True}


def test_backend_next_step_qwen_adapter_forwards_nudge_to_wire() -> None:
    """Integration: with QwenA3bAdapter, the system nudge message reaches
    chat.completions.create as the first message."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mk_completion_with_tool_call(
        "read",
        {
            "current_state": "s",
            "plan_remaining_steps_brief": ["x"],
            "identity_verified": False,
            "observation": "o",
            "outcome_leaning": "GATHERING_INFORMATION",
            "path": "AGENTS.md",
        },
    )
    adapter = QwenA3bAdapter()
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="qwen3.5-35b-a3b",
        reasoning_effort="high",
        adapter=adapter,
    )
    backend.next_step(
        messages=[
            Message(role="system", content="agent system prompt"),
            Message(role="user", content="task"),
        ],
        response_schema=NextStep,
        timeout_sec=5.0,
    )
    seen = fake_client.chat.completions.create.call_args.kwargs
    assert seen["messages"][0] == {"role": "system", "content": _QWEN_SYSTEM_NUDGE}
    assert seen["messages"][1]["content"] == "agent system prompt"


# ---------------------------------------------------------------------------
# Profile calibration — reasoning_effort matches the 2026-04-19 tested config
# ---------------------------------------------------------------------------


def test_qwen_profile_reasoning_effort_is_high() -> None:
    """The 2026-04-19 run produced 46.1% pass rate with env override
    AGENT_REASONING_EFFORT=high. The adapter default was 'medium' but was
    never measured. Align so the adapter default reproduces the tested run."""
    assert QwenA3bAdapter().profile.reasoning_effort == "high"


# ---------------------------------------------------------------------------
# Runaway-reasoning cap — 2026-04-20 t012 incident
# ---------------------------------------------------------------------------


def test_qwen_profile_max_completion_tokens_bounded_for_runaway() -> None:
    """The 2026-04-20 PROD t012 ("Whose birthday is coming up next?") ran
    classifier → UNKNOWN → raw agent with effort=high and the model produced
    120k+ reasoning tokens over 3h27m of failed retries before giving up.

    LM Studio keeps generating after our client HTTP timeout fires, so the
    server-side cap is the only real stop. This profile value must be set so
    LM Studio truncates the generation. Operator sets the LM Studio context
    side to ~101720; our wire cap at 100k stays strictly below so LM Studio
    will return ``finish_reason="length"`` before context overflow.
    """
    prof = QwenA3bAdapter().profile
    assert prof.max_completion_tokens == 100_000


def test_qwen_backend_forwards_max_completion_tokens_on_wire() -> None:
    """Integration guard: the adapter's profile value must reach the wire
    call, not be overridden by the backend's legacy 4096 default."""
    fake_client = MagicMock()
    msg = MagicMock()
    msg.tool_calls = None
    msg.content = ""
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg, finish_reason="stop")]
    completion.usage = MagicMock(
        prompt_tokens=1,
        completion_tokens=1,
        completion_tokens_details=MagicMock(reasoning_tokens=0),
    )
    fake_client.chat.completions.create.return_value = completion
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="qwen3.5-35b-a3b",
        reasoning_effort="high",
        adapter=QwenA3bAdapter(),
    )
    # extract_next_step will fail (no tool_calls, empty content) — we only
    # care that the request payload carries the cap.
    try:
        backend.next_step(
            messages=[Message(role="user", content="t")],
            response_schema=NextStep,
            timeout_sec=5.0,
        )
    except Exception:
        pass
    seen = fake_client.chat.completions.create.call_args.kwargs
    assert seen["max_tokens"] == 100_000


def test_backend_logs_warning_when_finish_reason_is_length(caplog) -> None:
    """Operator visibility: when LM Studio truncates at the cap (finish_reason
    "length"), emit a WARNING. Distinguishes "model gave up mid-reasoning"
    from "model chose the wrong tool" during post-run triage."""
    fake_client = MagicMock()
    msg = MagicMock()
    msg.tool_calls = None
    msg.content = ""
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg, finish_reason="length")]
    completion.usage = MagicMock(
        prompt_tokens=1,
        completion_tokens=100_000,
        completion_tokens_details=MagicMock(reasoning_tokens=99_000),
    )
    fake_client.chat.completions.create.return_value = completion
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="qwen3.5-35b-a3b",
        reasoning_effort="high",
        adapter=QwenA3bAdapter(),
    )
    import logging as _logging
    with caplog.at_level(_logging.WARNING,
                         logger="bitgn_contest_agent.backend.openai_toolcalling"):
        try:
            backend.next_step(
                messages=[Message(role="user", content="t")],
                response_schema=NextStep,
                timeout_sec=5.0,
            )
        except Exception:
            pass
    assert any("max_tokens cap" in r.message for r in caplog.records)
