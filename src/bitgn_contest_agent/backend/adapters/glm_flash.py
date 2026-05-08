"""Adapter for GLM-4.7-Flash-MLX (LM Studio).

Chain: standard tool_calls → envelope salvage. Critically **no** bare-
value salvage: GLM's short content-only replies include chat-template
leakage (``</tool_call>``, ``<|channel|>``, etc.) that the gpt-oss bare-
value path would package as answers (the 2026-04-19 score=0 incident).

Envelope salvage is safe because it requires a parseable JSON object
with a ``function.tool`` that matches a registered tool — template-leak
tokens never match that shape. GLM emits the full NextStep envelope as
content when it declines ``tool_choice="required"`` (observed
2026-04-19 14:09): skipping this salvage would burn the agent's entire
turn budget on critique/retry loops for structurally-valid replies.

Concurrency pinned to 1: GLM-4.7-Flash's memory footprint causes LM
Studio model-slot crashes at concurrency >1. MAX_PARALLEL_TASKS=3 has
been a live footgun — the profile makes safe concurrency the default.
"""
from __future__ import annotations

from typing import Any, Optional

from bitgn_contest_agent.schemas import NextStep

from .base import ModelAdapter, ModelProfile
from ._helpers import try_envelope


class GlmFlashAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="glm-4.7-flash-mlx",
            profile=ModelProfile(
                task_timeout_sec=3600,
                llm_http_timeout_sec=900,
                classifier_timeout_sec=600,
                max_parallel_tasks=1,
                max_inflight_llm=1,
                reasoning_effort="medium",
                lmstudio_host="localhost:1236",
            ),
        )

    def extract_next_step(self, message: Any) -> Optional[NextStep]:
        result = super().extract_next_step(message)
        if result is not None:
            return result
        content = getattr(message, "content", None) or ""
        return try_envelope(content)
