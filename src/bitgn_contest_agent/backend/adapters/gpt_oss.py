"""Adapter for openai/gpt-oss-20b (LM Studio MLX).

Per-model behavioral overrides (v0.1.25, 2026-04-23 120b evidence ported
to the 20b profile since both models share the same instruction-following
quirks):

1. ``format_retry_critique`` — imperative ``your NEXT tool_call MUST be X``
   prose for R0/R1/R5/R7 validator rejections. Gpt-oss rewords its
   justification under descriptive feedback instead of changing tool choice.
2. ``post_process_terminal`` — drop grounding_refs that were never read
   so hallucinated paths don't reject the whole terminal on R1.
3. ``extra_reactive_skills`` — load ``inbox-processing`` when task text
   contains inbox phrasing that the global tier1 regex misses.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, TYPE_CHECKING

from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion

if TYPE_CHECKING:
    from bitgn_contest_agent.session import Session

from .base import ModelAdapter, ModelProfile
from ._helpers import (
    gpt_oss_extra_reactive_skills,
    gpt_oss_filter_hallucinated_refs,
    gpt_oss_format_retry_critique,
    try_gpt_oss_full_chain,
)


class GptOssAdapter(ModelAdapter):
    """Full legacy salvage chain: harmony → bare-name → envelope → terminal → bare-value.

    Delegates content-based fallback to the pre-adapter helper so the
    existing test corpus stays green byte-for-byte.
    """

    def __init__(self) -> None:
        super().__init__(
            name="openai/gpt-oss-20b",
            profile=ModelProfile(
                task_timeout_sec=2400,
                llm_http_timeout_sec=600,
                classifier_timeout_sec=300,
                # p2i4: LM Studio MLX on a single local host cannot sustain
                # 4 parallel tasks × multi-step tool loops without KV-cache
                # pressure stalls. Two parallel tasks × up to 4 in-flight
                # LLM calls (prepass + classifier probes can overlap agent
                # step calls) is the user-validated conservative shape.
                # CLI flags still override; change defaults if a future
                # bench shows headroom.
                max_parallel_tasks=2,
                max_inflight_llm=4,
                reasoning_effort="high",
                lmstudio_host="localhost:1236",
            ),
        )

    def extract_next_step(self, message: Any) -> Optional[NextStep]:
        result = super().extract_next_step(message)
        if result is not None:
            return result
        content = getattr(message, "content", None) or ""
        return try_gpt_oss_full_chain(content)

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
