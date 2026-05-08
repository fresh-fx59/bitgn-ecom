"""Session state and loop detector.

One instance per task run. Lives in the worker thread. Never shared
across tasks (even within the same orchestrator run).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Tuple


_RECENT_WINDOW = 6
_REPEAT_THRESHOLD = 3


@dataclass(slots=True)
class Session:
    seen_refs: set[str] = field(default_factory=set)
    # Paths the agent attempted to read (regardless of success).
    # Used by R1 to distinguish "never tried" (REJECT) from
    # "tried but got not-found" (negative evidence, ACCEPT).
    attempted_reads: set[str] = field(default_factory=set)
    # Subset of attempted_reads where the adapter returned a
    # not-found error. An agent may legitimately cite such a
    # path in grounding_refs as negative evidence.
    verified_absent: set[str] = field(default_factory=set)
    rulebook_loaded: bool = False
    identity_loaded: bool = False
    step: int = 0
    recent_calls: Deque[Tuple[str, ...]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_WINDOW)
    )
    nudges_emitted: int = 0
    mutations: List[Tuple[str, str]] = field(default_factory=list)
    # Attachment paths extracted from outbox writes — used by terminal R5
    # to ensure every attachment was actually read before being cited.
    outbox_attachments: set[str] = field(default_factory=set)
    # Skill names loaded during this task — proactive (router.route) plus
    # reactive (mid-task injections). Validator uses this to key rules off
    # skill identity rather than paths, e.g. R7_INBOX_CLEANUP demands at
    # least one delete when the inbox-processing skill was loaded.
    skills_loaded: set[str] = field(default_factory=set)

    def loop_nudge_needed(self, call: Tuple[str, ...]) -> bool:
        """Record a (tool, canonical_args) tuple; return True if the same
        tuple has appeared _REPEAT_THRESHOLD times in the last _RECENT_WINDOW
        entries (i.e., this very call is the threshold-hitting one)."""
        self.recent_calls.append(call)
        count = sum(1 for c in self.recent_calls if c == call)
        return count >= _REPEAT_THRESHOLD

    def nudge_budget_remaining(self, *, max_nudges: int) -> int:
        return max(0, max_nudges - self.nudges_emitted)
