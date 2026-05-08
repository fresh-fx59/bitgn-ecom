"""Tests for GptOssRemoteAdapter — gpt-oss-120b on the neuraldeep gateway.

Evidence source: 2026-04-22 gpt-oss-120b PROD runs. The p15i20 run exhibited
a 13-task HTTP 408 cluster that took down the gateway window; restart at
p5i10 completed cleanly at 55/104 (52.9%). These tests pin the wiring that
makes a run without CLI overrides land on the validated p5i10 shape.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.backend.adapters import ADAPTERS, get_adapter
from bitgn_contest_agent.backend.adapters.gpt_oss import GptOssAdapter
from bitgn_contest_agent.backend.adapters.gpt_oss_remote import (
    GptOssRemoteAdapter,
)
from bitgn_contest_agent.schemas import NextStep


# ---------------------------------------------------------------------------
# Registry wiring — gpt-oss-120b must resolve to the remote adapter
# ---------------------------------------------------------------------------


def test_registry_maps_gpt_oss_120b_to_remote_adapter() -> None:
    """If this fails, a 120b run would use the LOCAL adapter which sets
    lmstudio_host=localhost:1236 and llm_http_timeout_sec=600. The watchdog
    would then try to unload a model that never loaded locally, and the
    600s HTTP timeout would wait ten minutes on gateway requests that the
    gateway's 60s cap already killed."""
    assert ADAPTERS["gpt-oss-120b"] is GptOssRemoteAdapter
    assert isinstance(get_adapter("gpt-oss-120b"), GptOssRemoteAdapter)


def test_registry_gpt_oss_20b_still_local_adapter() -> None:
    """gpt-oss-20b runs on LM Studio MLX and needs the watchdog host +
    long HTTP timeout. Must not be rerouted to the remote adapter."""
    assert ADAPTERS["openai/gpt-oss-20b"] is GptOssAdapter


# ---------------------------------------------------------------------------
# Profile — tuned to gateway realities
# ---------------------------------------------------------------------------


def test_remote_profile_matches_gateway_60s_cap() -> None:
    """Server-side cap is 60s; client must fail fast at ~65s so P2 retry
    re-issues instead of burning wall time waiting for an already-dead req.
    Wide tolerance so minor tuning doesn't break the test."""
    prof = GptOssRemoteAdapter().profile
    assert 60 <= prof.llm_http_timeout_sec <= 90, (
        "http timeout must be slightly past the 60s server cap"
    )
    assert 60 <= prof.classifier_timeout_sec <= 90


def test_remote_profile_no_lmstudio_host() -> None:
    """The 120b weights never touch local LM Studio. Leaving
    lmstudio_host=None disables the watchdog-unload path in the backend
    (there is no model to unload) and prevents spurious SDK calls."""
    assert GptOssRemoteAdapter().profile.lmstudio_host is None


def test_remote_profile_prod_validated_concurrency() -> None:
    """The 2026-04-22 p15i20 run hit a 13-task HTTP 408 cluster. The
    p5i10 restart completed cleanly. Defaults match the validated shape."""
    prof = GptOssRemoteAdapter().profile
    assert prof.max_parallel_tasks == 5
    assert prof.max_inflight_llm == 10


def test_remote_profile_reasoning_high() -> None:
    """Parity with local 20b gpt-oss: the agent loop depends on
    reasoning_effort=high for chain-of-thought heavy tasks."""
    assert GptOssRemoteAdapter().profile.reasoning_effort == "high"


def test_remote_profile_max_completion_tokens_cap() -> None:
    """Cap runaway reasoning before the gateway's 60s wall-clock cap
    returns truncated garbage. 100k matches qwen-a3b parity."""
    assert GptOssRemoteAdapter().profile.max_completion_tokens == 100_000


# ---------------------------------------------------------------------------
# Salvage chain reused from the local gpt-oss adapter
# ---------------------------------------------------------------------------


def _mk_message(*, tool_calls: list | None = None, content: str = "") -> MagicMock:
    msg = MagicMock()
    msg.tool_calls = tool_calls
    msg.content = content
    return msg


def test_remote_extract_delegates_to_gpt_oss_full_chain() -> None:
    """No 120b-specific salvage miss was observed in the 2026-04-22 run,
    so behavior is preserved: same harmony/bare-name/envelope/terminal/
    bare-value chain as the local adapter."""
    harmony = (
        '<|start|>assistant<|channel|>commentary to=functions.read '
        '<|constrain|>json<|message|>{"current_state": "s", '
        '"plan_remaining_steps_brief": ["x"], "identity_verified": false, '
        '"observation": "o", "outcome_leaning": "GATHERING_INFORMATION", '
        '"path": "AGENTS.md"}<|call|>'
    )
    msg = _mk_message(tool_calls=None, content=harmony)
    ns = GptOssRemoteAdapter().extract_next_step(msg)
    assert ns is not None
    assert isinstance(ns, NextStep)
    assert ns.function.tool == "read"


def test_remote_extract_standard_tool_calls_path() -> None:
    """When the gateway returns well-formed tool_calls (the common case),
    the adapter must use the standard parent-class path and never touch
    the salvage chain."""
    import json as _json

    tc = MagicMock()
    tc.function.name = "read"
    tc.function.arguments = _json.dumps({
        "current_state": "s",
        "plan_remaining_steps_brief": ["x"],
        "identity_verified": False,
        "observation": "o",
        "outcome_leaning": "GATHERING_INFORMATION",
        "path": "AGENTS.md",
    })
    msg = _mk_message(tool_calls=[tc], content="")
    ns = GptOssRemoteAdapter().extract_next_step(msg)
    assert ns is not None
    assert ns.function.tool == "read"
