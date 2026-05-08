"""Adapter for qwen3.6-35b-a3b served via the neuraldeep LiteLLM gateway.

Differences from the local ``QwenA3bAdapter`` (LM Studio MLX):

1. **Reasoning knob swap.** The 2026-04-20 qwen3.6 PROD run scored 18/104
   (17.3%) with ``reasoning_tokens=0`` across every completion. Direct
   probes against the gateway confirm the cause: ``extra_body={"reasoning":
   {"effort": "high"}}`` is silently dropped — qwen3.6 returns empty
   content. The flag the deployment actually honors is
   ``extra_body={"thinking": True}``; the same prompt then produces
   substantive content. ``shape_request`` swaps the knob before the wire
   write.

2. **60-second server cap.** The gateway enforces an internal 60s
   timeout (LiteLLM ``request_timeout``) that surfaces as HTTP 408
   ``litellm.Timeout: APITimeout``. Client ``llm_http_timeout_sec=65``
   fails fast just past the cap instead of waiting minutes on a dead
   request — the P2 retry wrapper then re-issues.

3. **Conservative concurrency.** The PROD run at max_parallel=10 /
   max_inflight=20 hit 191× HTTP 502 Bad Gateway (13% of calls) from
   gateway instability. Dropped to 4/4; still 4× the local default
   since latency is single-digit seconds.

``reasoning_tokens`` stays 0 on this gateway even with ``thinking:
True`` — the gateway strips the counter from the usage block. Verify
reasoning is live by inspecting ``choices[0].message.content`` for
substantive reasoning prose (not the zero counter).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from bitgn_contest_agent.schemas import NextStep

from ._helpers import (
    gpt_oss_format_retry_critique,
    try_envelope,
    try_qwen_bare_answer,
)
from .base import ModelAdapter, ModelProfile
from .qwen_a3b import _QWEN_SYSTEM_NUDGE

if TYPE_CHECKING:
    from bitgn_contest_agent.session import Session


class QwenA3bRemoteAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="qwen3.6-35b-a3b",
            profile=ModelProfile(
                task_timeout_sec=2400,
                llm_http_timeout_sec=65,
                classifier_timeout_sec=65,
                max_parallel_tasks=4,
                max_inflight_llm=4,
                reasoning_effort="high",
                # Parity with local qwen3.5: cap runaway reasoning. The
                # neuraldeep gateway's 60s server cap enforces wall-clock
                # independently, but a token cap still protects against
                # the gateway returning a 120k-token blob in one burst.
                max_completion_tokens=100_000,
            ),
        )

    def shape_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = list(payload.get("messages") or [])
        extra_body = dict(payload.get("extra_body") or {})
        # Gateway drops reasoning.effort — strip it to avoid sending a
        # dead flag, then set the flag this deployment honors.
        extra_body.pop("reasoning", None)
        extra_body["thinking"] = True
        # neuraldeep/LiteLLM rejects payloads with two system messages
        # (error: "System message must be at the beginning"). Merge the
        # nudge into the existing system message rather than prepending
        # a second one. For payloads with no system message (classifier
        # probes, tests), prepend a fresh one.
        if messages and messages[0].get("role") == "system":
            first = dict(messages[0])
            first["content"] = f"{_QWEN_SYSTEM_NUDGE}\n\n{first.get('content', '')}"
            shaped_messages = [first, *messages[1:]]
        else:
            shaped_messages = [
                {"role": "system", "content": _QWEN_SYSTEM_NUDGE},
                *messages,
            ]
        return {
            **payload,
            "messages": shaped_messages,
            "extra_body": extra_body,
        }

    def extract_next_step(self, message: Any) -> Optional[NextStep]:
        result = super().extract_next_step(message)
        if result is not None:
            return result
        content = getattr(message, "content", None) or ""
        parsed = try_envelope(content)
        if parsed is not None:
            return parsed
        return try_qwen_bare_answer(content)

    def format_retry_critique(
        self,
        reasons: Sequence[str],
        session: "Session",
    ) -> str:
        """Reuse the imperative critique helper. Despite the
        ``gpt_oss_`` prefix the patterns are model-agnostic (R7 inbox
        cleanup, R0 min-explore, R6 mutation discipline, R1 unread refs,
        R5 outbox attachment). 2026-05-01 qwen3.6/neuraldeep PROD run:
        terminals re-emitted the same shape under descriptive critiques
        and terminated via submit_anyway. The imperative wording forces
        a tool-call switch instead.
        """
        return gpt_oss_format_retry_critique(reasons)
