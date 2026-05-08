"""Adapter for qwen3.5-35b-a3b (Heretic MXFP4, LM Studio).

Chain: standard tool_calls → envelope salvage → bare-answer salvage.

Envelope salvage handles qwen's structured content-only replies —
the full NextStep envelope emitted as free-text when the server
declines ``tool_choice="required"`` (observed 2026-04-19 20:09 on
PROD t000: envelope with ``"function": {"tool": "read", ...}`` as
content body).

Bare-answer salvage handles qwen's habit of emitting a short
literal terminal answer as plain content — numbers ("1170"),
dates ("03-02-2026"), short file-path lists. 12 cases observed in
the 2026-04-19 PROD run; 6 were in failed tasks that the circuit
breaker closed with OUTCOME_NONE_UNSUPPORTED. Strong guards (see
``_helpers.try_qwen_bare_answer``) keep this from hijacking a
GATHERING_INFORMATION turn.

``shape_request`` prepends a terse system nudge addressing the
empty-content failure mode: in 196/211 salvage_misses (93%) on
the 2026-04-19 run qwen returned ``content=""`` and
``tool_calls=None`` — correlating with ``reasoning_tokens=0`` on
~30% of steps. Reminding the model that its only valid output
shape is one tool_call reduces these empty returns.

``reasoning_effort="high"`` matches the env-override used during
the 2026-04-19 run. The pre-adapter default was ``medium`` but
was never measured; aligning so the default IS the tested config
(reproducibility over speculation).

Concurrency pinned at 1. The 2026-04-19 p2i2 run finished without
slot crashes, but later PROD runs at p2i2 exhibited LM Studio memory
pressure symptoms (watchdog fires, unloaded-model 400s) concentrated
on overlapping-slot windows. AGENTS.md makes p1i1 mandatory for local
LM Studio qwen3.5; the adapter default now matches so a run without
CLI overrides is also safe.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from bitgn_contest_agent.schemas import NextStep

from .base import ModelAdapter, ModelProfile
from ._helpers import try_envelope, try_qwen_bare_answer


_QWEN_SYSTEM_NUDGE = (
    "Output discipline: every turn you MUST emit exactly one OpenAI "
    "tool_call. Never reply with empty content or free prose. If you "
    "are terminating, call report_completion. Otherwise call one of "
    "the read/list/tree/search/context tools. Content-only replies "
    "will be rejected."
)


class QwenA3bAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="qwen3.5-35b-a3b",
            profile=ModelProfile(
                task_timeout_sec=1800,
                llm_http_timeout_sec=600,
                classifier_timeout_sec=300,
                max_parallel_tasks=1,
                max_inflight_llm=1,
                reasoning_effort="high",
                # 100k token cap: the 2026-04-20 PROD run saw t012 runaway
                # reasoning past 120k tokens while our 600s HTTP client had
                # already given up — LM Studio kept generating. Paired with
                # the operator setting the LM Studio context cap to 101720,
                # this bounds worst-case generation on the wire.
                max_completion_tokens=100_000,
                lmstudio_host="localhost:1236",
                # Classifier probe runs at effort="medium" to avoid runaway
                # CoT on a 3-way routing question. 2026-04-22 PROD p1i1 saw
                # 7 watchdog fires at step 1 (skill=-, category=UNKNOWN)
                # where qwen3.5 thought through the classifier prompt for
                # minutes at effort="high". Agent path stays at high.
                classifier_reasoning_effort="medium",
            ),
        )

    def shape_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = list(payload.get("messages") or [])
        nudge = {"role": "system", "content": _QWEN_SYSTEM_NUDGE}
        return {**payload, "messages": [nudge, *messages]}

    def extract_next_step(self, message: Any) -> Optional[NextStep]:
        result = super().extract_next_step(message)
        if result is not None:
            return result
        content = getattr(message, "content", None) or ""
        parsed = try_envelope(content)
        if parsed is not None:
            return parsed
        return try_qwen_bare_answer(content)
