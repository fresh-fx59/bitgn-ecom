"""Failure triage clustering — deterministic, order-sensitive.

Evaluation order is FIXED and load-bearing: the first matching rule
wins. This prevents ambiguous cases from drifting between categories
between runs. Clusters:

  inbox         — agent skipped /inbox/ identity lookup
  wrong_action  — grader failed because agent took the wrong tool path
  false_refusal — OUTCOME_DENIED_SECURITY on a non-security task
  timeout       — trial exceeded time budget
  calendar      — calendar/scheduler task failed on temporal grounding
  other         — fallback
"""
from __future__ import annotations

from typing import Any

TRIAGE_ORDER: tuple[str, ...] = (
    "inbox",
    "wrong_action",
    "false_refusal",
    "timeout",
    "calendar",
    "other",
)


def _any_text_contains(evidence: dict[str, Any], *needles: str) -> bool:
    texts = evidence.get("step_texts") or []
    blob = " ".join(t.lower() for t in texts if t)
    return any(n.lower() in blob for n in needles)


def classify_failure(evidence: dict[str, Any]) -> str:
    """Return the first-matching cluster name from TRIAGE_ORDER."""
    # inbox: reasoning mentions missing inbox identity lookup
    if _any_text_contains(evidence, "/inbox/", "inbox identity",
                           "forgot to check /inbox"):
        return "inbox"
    # wrong_action: grader failed despite OUTCOME_OK, reasoning mentions
    # the agent took a different tool path than required
    if (evidence.get("grader_failed")
            and evidence.get("outcome") == "OUTCOME_OK"
            and _any_text_contains(evidence, "instead of", "wrong tool",
                                    "writing email draft",
                                    "scheduler call")):
        return "wrong_action"
    # false_refusal: denied security on a non-security task
    if (evidence.get("outcome") == "OUTCOME_DENIED_SECURITY"
            and evidence.get("task_category") not in ("security",)):
        return "false_refusal"
    # timeout: explicit timeout flag or latency over 3 min
    if evidence.get("timed_out") or (evidence.get("latency_ms", 0) >= 180_000):
        return "timeout"
    # calendar: failed calendar task
    if evidence.get("task_category") == "calendar" and evidence.get("grader_failed"):
        return "calendar"
    return "other"
