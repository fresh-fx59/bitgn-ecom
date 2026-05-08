"""Adapter for gpt-oss-120b served via the neuraldeep LiteLLM gateway.

Differences from the local ``GptOssAdapter`` (LM Studio MLX, 20b):

1. **No LM Studio watchdog host.** The 120b weights never touch local
   LM Studio; there is no model to unload on wallclock overrun. Leaving
   ``lmstudio_host=None`` (the base default) disables the watchdog path
   in ``OpenAIToolCallingBackend`` for remote completions.

2. **60-second server cap.** The gateway enforces an internal 60s
   timeout (LiteLLM ``request_timeout``) that surfaces as HTTP 408
   ``litellm.Timeout: APITimeout``. Client ``llm_http_timeout_sec=65``
   fails fast just past the cap so the P2 retry wrapper re-issues
   instead of waiting the local 600s on a request the gateway has
   already killed. ``classifier_timeout_sec`` matches for the same
   reason.

3. **Concurrency at 5/10.** The 2026-04-22 PROD run at p15i20 hit a
   13-crash HTTP 408 cluster; restarted at p5i10 and completed cleanly
   at 55/104 (52.9%). The PROD-validated p5i10 is the adapter default
   so a run without CLI overrides is also safe.

4. **Salvage chain unchanged.** Neuraldeep transports the same OpenAI
   tool_calls wire format as LM Studio, and no 120b-specific salvage
   miss was observed in the 2026-04-22 run. ``extract_next_step``
   delegates to ``try_gpt_oss_full_chain`` for parity with the local
   20b adapter.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, TYPE_CHECKING

from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion

if TYPE_CHECKING:
    from bitgn_contest_agent.session import Session

from ._helpers import (
    gpt_oss_extra_reactive_skills,
    gpt_oss_filter_hallucinated_refs,
    gpt_oss_format_retry_critique,
    try_gpt_oss_full_chain,
)
from .base import ModelAdapter, ModelProfile


class GptOssRemoteAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="gpt-oss-120b",
            profile=ModelProfile(
                task_timeout_sec=2400,
                llm_http_timeout_sec=65,
                classifier_timeout_sec=65,
                max_parallel_tasks=5,
                max_inflight_llm=10,
                reasoning_effort="high",
                max_completion_tokens=100_000,
            ),
        )

    def extract_next_step(self, message: Any) -> Optional[NextStep]:
        result = super().extract_next_step(message)
        if result is not None:
            return result
        content = getattr(message, "content", None) or ""
        return try_gpt_oss_full_chain(content)

    # 2026-04-23 v0.1.25 behavioral hooks — see ``gpt_oss.py`` for the
    # 20b docstring; the 120b shares the same instruction-following
    # quirks so it reuses the same helpers.
    def format_retry_critique(
        self,
        reasons: Sequence[str],
        session: "Session",
    ) -> str:
        return gpt_oss_format_retry_critique(reasons)

    def post_process_terminal(
        self,
        fn: ReportTaskCompletion,
        session: "Session",
    ) -> ReportTaskCompletion:
        return gpt_oss_filter_hallucinated_refs(fn, session)

    def extra_reactive_skills(self, task_text: str) -> frozenset[str]:
        return gpt_oss_extra_reactive_skills(task_text)
