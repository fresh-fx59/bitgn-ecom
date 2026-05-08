"""Smoke-test task list and hardcoded operating point.

The smoke suite is a fast-fail signal, not a measurement. All knobs
are fixed so any smoke failure is unambiguous.

The PAC1 smoke set was anchored on calendar/security/clarification
tasks. ECOM smoke selection is TBD — until anchor tasks are picked
from `bitgn/ecom1-dev`, the smoke suite is the first five trials in
the benchmark's natural order. Override via `bitgn-agent run-benchmark
--smoke` after the canonical anchor IDs are known.
"""
from __future__ import annotations

SMOKE_TASKS: list[str] = ["t01", "t02", "t03", "t04", "t05"]
SMOKE_CEILING_SEC: int = 180
SMOKE_MAX_PARALLEL: int = 5
SMOKE_MAX_INFLIGHT_LLM: int = 8
