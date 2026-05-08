"""Tests for the per-adapter ``classifier_reasoning_effort`` knob.

Evidence: 2026-04-22 qwen3.5 local p1i1 PROD run, 7 watchdog fires all at
step 1 (skill=-, category=UNKNOWN) where the classifier probe wedged on
runaway CoT at effort="high". Splitting the classifier knob lets agents
keep high-effort reasoning while structured probes run cheaper.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.backend.adapters import get_adapter
from bitgn_contest_agent.backend.adapters.base import ModelProfile
from bitgn_contest_agent.backend.adapters.gpt_oss import GptOssAdapter
from bitgn_contest_agent.backend.adapters.qwen_a3b import QwenA3bAdapter


def test_profile_classifier_reasoning_effort_defaults_to_none() -> None:
    """Default = None so adapters that don't opt in inherit the agent
    effort (preserving legacy behavior)."""
    prof = ModelProfile(
        task_timeout_sec=1,
        llm_http_timeout_sec=1,
        classifier_timeout_sec=1,
        max_parallel_tasks=1,
        max_inflight_llm=1,
        reasoning_effort="high",
    )
    assert prof.classifier_reasoning_effort is None


def test_qwen_a3b_classifier_effort_is_medium() -> None:
    """Qwen3.5 local: step-1 classifier wedge fix pins the classifier to
    medium so the 3-way routing prompt doesn't trigger runaway CoT."""
    assert QwenA3bAdapter().profile.classifier_reasoning_effort == "medium"
    # Agent path still runs at high — depth matters for the agent loop.
    assert QwenA3bAdapter().profile.reasoning_effort == "high"


def test_gpt_oss_classifier_effort_inherits() -> None:
    """gpt-oss-20b classifier is fast; no evidence of runaway CoT on
    the routing probe. Leave as None = inherit agent effort."""
    assert GptOssAdapter().profile.classifier_reasoning_effort is None


def test_call_structured_uses_classifier_effort_when_set() -> None:
    """Integration: call_structured sends ``extra_body.reasoning.effort``
    from the classifier knob, not from the agent knob, when the knob is set.
    """
    from bitgn_contest_agent.backend.openai_toolcalling import (
        OpenAIToolCallingBackend,
    )
    from pydantic import BaseModel

    class _Out(BaseModel):
        category: str = "X"

    fake_client = MagicMock()
    parsed = MagicMock()
    parsed.choices = [MagicMock(message=MagicMock(parsed=_Out(), content=""))]
    fake_client.beta.chat.completions.parse.return_value = parsed

    adapter = QwenA3bAdapter()
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="qwen3.5-35b-a3b",
        reasoning_effort="high",
        adapter=adapter,
    )
    backend.call_structured("probe", _Out, timeout_sec=5.0)
    kwargs = fake_client.beta.chat.completions.parse.call_args.kwargs
    # Classifier knob wins over the agent's "high".
    assert kwargs["extra_body"]["reasoning"]["effort"] == "medium"


def test_call_structured_falls_back_to_agent_effort() -> None:
    """When classifier_reasoning_effort is None, call_structured uses the
    agent effort — backward compatible for gpt-oss, glm-flash, lfm2."""
    from bitgn_contest_agent.backend.openai_toolcalling import (
        OpenAIToolCallingBackend,
    )
    from pydantic import BaseModel

    class _Out(BaseModel):
        category: str = "X"

    fake_client = MagicMock()
    parsed = MagicMock()
    parsed.choices = [MagicMock(message=MagicMock(parsed=_Out(), content=""))]
    fake_client.beta.chat.completions.parse.return_value = parsed

    adapter = GptOssAdapter()  # classifier_reasoning_effort = None
    backend = OpenAIToolCallingBackend(
        client=fake_client,
        model="openai/gpt-oss-20b",
        reasoning_effort="high",
        adapter=adapter,
    )
    backend.call_structured("probe", _Out, timeout_sec=5.0)
    kwargs = fake_client.beta.chat.completions.parse.call_args.kwargs
    assert kwargs["extra_body"]["reasoning"]["effort"] == "high"
