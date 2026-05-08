"""Adapter for liquid/lfm2-24b-a2b (LM Studio MLX).

LFM2 is trained to emit the bare OpenAI tool-call shape
(``{"name": "...", "arguments": {...}}``) as free-text content when the
server does not honor ``tool_choice="required"``. Chain: standard →
bare-name-arguments. No harmony, envelope, or bare-value (those are
gpt-oss shapes; applying them here could misfire the same way the
bare-value salvage misfires on GLM).
"""
from __future__ import annotations

from typing import Any, Optional

from bitgn_contest_agent.schemas import NextStep

from .base import ModelAdapter, ModelProfile
from ._helpers import try_bare_name_arguments


class Lfm2Adapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="liquid/lfm2-24b-a2b",
            profile=ModelProfile(
                task_timeout_sec=1800,
                llm_http_timeout_sec=600,
                classifier_timeout_sec=300,
                max_parallel_tasks=2,
                max_inflight_llm=2,
                reasoning_effort="medium",
                lmstudio_host="localhost:1236",
            ),
        )

    def extract_next_step(self, message: Any) -> Optional[NextStep]:
        result = super().extract_next_step(message)
        if result is not None:
            return result
        content = getattr(message, "content", None) or ""
        return try_bare_name_arguments(content)
