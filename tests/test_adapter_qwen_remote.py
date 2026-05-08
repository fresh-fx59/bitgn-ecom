"""Tests for QwenA3bRemoteAdapter — qwen3.6 on the neuraldeep LiteLLM gateway.

Evidence source: 2026-04-20 qwen3.6-35b-a3b PROD run + 2026-04-20 live probes
against ``https://api.neuraldeep.ru/v1``. Each test cites the empirical
gateway behavior it guards against.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.backend.adapters import ADAPTERS, get_adapter
from bitgn_contest_agent.backend.adapters.qwen_a3b import (
    QwenA3bAdapter,
    _QWEN_SYSTEM_NUDGE,
)
from bitgn_contest_agent.backend.adapters.qwen_a3b_remote import (
    QwenA3bRemoteAdapter,
)
from bitgn_contest_agent.backend.base import Message
from bitgn_contest_agent.backend.openai_toolcalling import (
    OpenAIToolCallingBackend,
)
from bitgn_contest_agent.schemas import NextStep


# ---------------------------------------------------------------------------
# Registry wiring — qwen3.6 must resolve to the remote adapter, not local
# ---------------------------------------------------------------------------


def test_registry_maps_qwen36_to_remote_adapter() -> None:
    """If this fails, a qwen3.6 run would use the LOCAL adapter and send
    extra_body.reasoning.effort — which the gateway silently drops, causing
    empty-content replies and the 17.3% pass-rate regression seen 2026-04-20."""
    assert ADAPTERS["qwen3.6-35b-a3b"] is QwenA3bRemoteAdapter
    # Still resolved through the public registry API.
    assert isinstance(get_adapter("qwen3.6-35b-a3b"), QwenA3bRemoteAdapter)


def test_registry_qwen35_still_local_adapter() -> None:
    """qwen3.5 on LM Studio MLX honors reasoning.effort and must not be
    rerouted to the remote adapter by accident."""
    assert ADAPTERS["qwen3.5-35b-a3b"] is QwenA3bAdapter


# ---------------------------------------------------------------------------
# shape_request: reasoning → thinking swap + system nudge
# ---------------------------------------------------------------------------


def test_remote_shape_request_swaps_reasoning_for_thinking() -> None:
    """The 2026-04-20 probe showed extra_body={'reasoning': {'effort':
    'high'}} returns empty content; extra_body={'thinking': True} returns
    substantive content. The swap must happen inside the adapter, not at the
    call site — the backend always constructs reasoning.effort payload first."""
    adapter = QwenA3bRemoteAdapter()
    shaped = adapter.shape_request({
        "model": "qwen3.6-35b-a3b",
        "messages": [{"role": "user", "content": "t"}],
        "extra_body": {"reasoning": {"effort": "high"}},
    })
    # reasoning flag stripped: it's dead weight on this gateway.
    assert "reasoning" not in shaped["extra_body"]
    # thinking flag inserted: the knob this deployment actually honors.
    assert shaped["extra_body"]["thinking"] is True


def test_remote_shape_request_preserves_other_extra_body_keys() -> None:
    """If the adapter wipes extra_body it would break any future flag
    (response_format, temperature overrides wired via extra_body, etc.)."""
    adapter = QwenA3bRemoteAdapter()
    shaped = adapter.shape_request({
        "messages": [],
        "extra_body": {"reasoning": {"effort": "high"}, "custom_flag": 42},
    })
    assert shaped["extra_body"]["custom_flag"] == 42
    assert shaped["extra_body"]["thinking"] is True
    assert "reasoning" not in shaped["extra_body"]


def test_remote_shape_request_handles_missing_extra_body() -> None:
    """Classifier paths and test doubles may send no extra_body. Must not crash."""
    adapter = QwenA3bRemoteAdapter()
    shaped = adapter.shape_request({"messages": []})
    assert shaped["extra_body"] == {"thinking": True}


def test_remote_shape_request_merges_nudge_into_existing_system() -> None:
    """The neuraldeep gateway rejects two system messages with HTTP 400
    'System message must be at the beginning' (seen 2026-04-20 on t002).
    When the agent already supplies a system prompt, merge the nudge into
    its content rather than prepending a second system message."""
    adapter = QwenA3bRemoteAdapter()
    original_messages = [
        {"role": "system", "content": "agent system prompt"},
        {"role": "user", "content": "task"},
    ]
    shaped = adapter.shape_request({
        "messages": original_messages,
        "extra_body": {"reasoning": {"effort": "high"}},
    })
    # Exactly one system message — at position 0 — carrying BOTH the
    # nudge (first) and the original agent prompt (after).
    system_roles = [m for m in shaped["messages"] if m["role"] == "system"]
    assert len(system_roles) == 1
    assert shaped["messages"][0]["role"] == "system"
    assert _QWEN_SYSTEM_NUDGE in shaped["messages"][0]["content"]
    assert "agent system prompt" in shaped["messages"][0]["content"]
    # Nudge comes first so its discipline framing is the lead instruction.
    nudge_idx = shaped["messages"][0]["content"].index(_QWEN_SYSTEM_NUDGE)
    prompt_idx = shaped["messages"][0]["content"].index("agent system prompt")
    assert nudge_idx < prompt_idx
    # Remaining messages untouched.
    assert shaped["messages"][1:] == original_messages[1:]


def test_remote_shape_request_prepends_nudge_when_no_system_message() -> None:
    """Classifier probes and test doubles may send no system message.
    In that case prepend a fresh one — only safe because there is no
    existing system message to collide with."""
    adapter = QwenA3bRemoteAdapter()
    shaped = adapter.shape_request({
        "messages": [{"role": "user", "content": "task"}],
    })
    assert shaped["messages"][0] == {"role": "system", "content": _QWEN_SYSTEM_NUDGE}
    assert shaped["messages"][1] == {"role": "user", "content": "task"}
    # Still exactly one system message.
    system_count = sum(1 for m in shaped["messages"] if m["role"] == "system")
    assert system_count == 1


def test_remote_shape_request_does_not_mutate_input() -> None:
    adapter = QwenA3bRemoteAdapter()
    payload = {
        "messages": [{"role": "user", "content": "t"}],
        "extra_body": {"reasoning": {"effort": "high"}, "custom": 1},
    }
    payload_snapshot = json.loads(json.dumps(payload))
    adapter.shape_request(payload)
    assert payload == payload_snapshot


# ---------------------------------------------------------------------------
# Profile — tuned to gateway realities
# ---------------------------------------------------------------------------


def test_remote_profile_matches_gateway_60s_cap() -> None:
    """Server-side cap is 60s; client must fail fast at ~65s so P2 retry
    re-issues instead of burning wall time waiting for an already-dead req.
    Wide tolerance so minor tuning doesn't break the test."""
    prof = QwenA3bRemoteAdapter().profile
    assert 60 <= prof.llm_http_timeout_sec <= 90, (
        "http timeout must be slightly past the 60s server cap"
    )
    assert 60 <= prof.classifier_timeout_sec <= 90


def test_remote_profile_conservative_concurrency() -> None:
    """The 2026-04-20 PROD run at 10×20 hit 191 HTTP 502. Conservative
    concurrency reduces gateway pressure. Upper bound checked so future
    tuning stays within the range that didn't crash the gateway."""
    prof = QwenA3bRemoteAdapter().profile
    assert prof.max_parallel_tasks <= 6
    assert prof.max_inflight_llm <= 6


def test_remote_profile_max_completion_tokens_matches_local() -> None:
    """Runaway-reasoning cap: same failure mode as local qwen (high-effort
    CoT blowing past wall-clock budgets). Keep parity with local so behavior
    is predictable across runtime swaps."""
    assert QwenA3bRemoteAdapter().profile.max_completion_tokens == 100_000


# ---------------------------------------------------------------------------
# Salvage chain reused from the local qwen adapter
# ---------------------------------------------------------------------------


def _mk_message(*, tool_calls: list[Any] | None = None, content: str = "") -> MagicMock:
    msg = MagicMock()
    msg.tool_calls = tool_calls
    msg.content = content
    return msg


def test_remote_extract_envelope_salvage_identical_to_local() -> None:
    """Same qwen family, same envelope-as-content quirk — the remote adapter
    must also salvage it, not just the local one."""
    envelope = {
        "current_state": "starting",
        "plan_remaining_steps_brief": ["read AGENTS"],
        "identity_verified": False,
        "observation": "first turn",
        "outcome_leaning": "GATHERING_INFORMATION",
        "function": {"tool": "read", "path": "AGENTS.md"},
    }
    msg = _mk_message(tool_calls=None, content=json.dumps(envelope))
    ns = QwenA3bRemoteAdapter().extract_next_step(msg)
    assert ns is not None
    assert ns.function.tool == "read"


def test_remote_extract_bare_answer_salvage() -> None:
    msg = _mk_message(tool_calls=None, content="1170")
    ns = QwenA3bRemoteAdapter().extract_next_step(msg)
    assert ns is not None
    assert ns.function.tool == "report_completion"
    assert ns.function.outcome == "OUTCOME_OK"


# ---------------------------------------------------------------------------
# Backend wiring — shaped request reaches chat.completions.create
# ---------------------------------------------------------------------------


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


def test_backend_forwards_thinking_flag_on_wire() -> None:
    """Integration: end-to-end, a qwen3.6 backend call must send
    extra_body.thinking=True and NOT extra_body.reasoning to the server."""
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
    adapter = QwenA3bRemoteAdapter()
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="qwen3.6-35b-a3b",
        reasoning_effort="high",
        adapter=adapter,
    )
    backend.next_step(
        messages=[Message(role="user", content="t")],
        response_schema=NextStep,
        timeout_sec=5.0,
    )
    seen = fake_client.chat.completions.create.call_args.kwargs
    assert "reasoning" not in seen["extra_body"]
    assert seen["extra_body"]["thinking"] is True
    # System nudge first, per parent-class contract.
    assert seen["messages"][0]["content"] == _QWEN_SYSTEM_NUDGE
