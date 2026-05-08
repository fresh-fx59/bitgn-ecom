"""Post-hoc divergence analyzer.

Tertiary signal only. A keyword hit indicates the agent surfaced a
rule-like phrase in its step reasoning — it does NOT prove the agent
followed a rule, and absence does not prove it didn't. Never used as a
pass/fail gate; consumed only by bench_summary as divergence_count.
"""
from __future__ import annotations

from typing import Optional, Sequence

# Keyword list maps loosely to the six runtime-rule categories. Keep
# lowercased for cheap matching. Add rarely — false positives are worse
# than false negatives for a tertiary signal.
_KEYWORDS: tuple[str, ...] = (
    # authority
    "agents.md",
    "model spec",
    "levels of authority",
    # conflict / refusal resolution
    "user instruction contradicts",
    "conflict between",
    # security
    "outcome_denied_security",
    "prompt injection",
    # inbox identity
    "/inbox/",
    "inbox identity",
    # erc3 pre-pass
    "erc3",
    "erc-3",
    # temporal grounding
    "current date",
    "today's date",
)


def is_divergent_step(text: Optional[str]) -> bool:
    """Return True iff the step's free-form text contains any rule keyword."""
    if not text:
        return False
    haystack = text.lower()
    return any(k in haystack for k in _KEYWORDS)


def count_divergent_steps(step_texts: Sequence[Optional[str]]) -> int:
    return sum(1 for t in step_texts if is_divergent_step(t))
