"""Smoke-test task list and hardcoded operating point.

The smoke suite is a fast-fail signal, not a measurement. All knobs
are fixed so any smoke failure is unambiguous.

Task selection rationale (documented for Phase 2+ reference):
  t02 — calendar scheduling (temporal grounding)
  t42 — temporal coherence
  t41 — calendar scheduling (second variant)
  t15 — OUTCOME_DENIED_SECURITY control (expected to fail on merit)
  t43 — clarification flow
"""
from __future__ import annotations

SMOKE_TASKS: list[str] = ["t02", "t42", "t41", "t15", "t43"]
SMOKE_CEILING_SEC: int = 180
SMOKE_MAX_PARALLEL: int = 5
SMOKE_MAX_INFLIGHT_LLM: int = 8
