"""Per-run orchestration metrics.

Tracks peak in-flight LLM calls and rate-limit error counts for the
tuned-baseline gate in Phase 2 and the Phase 3 comparison. Pure
collector — does not touch the LLM loop directly. Agents call into
it via hooks around the backend.next_step call cycle.
"""
from __future__ import annotations

import threading
from typing import Any, Dict


class RunMetrics:
    def __init__(self, *, max_inflight_llm: int) -> None:
        self._lock = threading.Lock()
        self._inflight = 0
        self.max_inflight_llm = max_inflight_llm
        self.peak_inflight_llm = 0
        self.rate_limit_errors = 0

    def on_call_start(self) -> None:
        with self._lock:
            self._inflight += 1
            if self._inflight > self.peak_inflight_llm:
                self.peak_inflight_llm = self._inflight

    def on_call_end(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def on_rate_limit_error(self) -> None:
        with self._lock:
            self.rate_limit_errors += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "max_inflight_llm": self.max_inflight_llm,
                "peak_inflight_llm": self.peak_inflight_llm,
                "rate_limit_errors": self.rate_limit_errors,
            }
