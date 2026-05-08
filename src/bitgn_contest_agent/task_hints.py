"""Task-specific hardcode hints for known PROD failure patterns.

Motivation
----------
The PAC1 lineage of this file shipped three surgical matchers fitted to
specific PROD failure clusters discovered after the 2026-04-11 run. Those
matchers were tuned to vault-shaped task text ("queue up these docs for
migration to my NORA", "last recorded message from", "start date of
project") and have no analogue on ECOM, so they are dropped wholesale.

This module is kept (rather than deleted) because the hint-injection
pattern itself — narrow regex over task_text, returns Optional[str], one
ADDITIONAL `role=user` message after the task text, system-prompt cache
preserved — is a load-bearing reliability lever that ECOM-specific
matchers should follow exactly. Add new matchers here as failure
clusters emerge from real ECOM runs; do NOT pre-emptively author hints
against imagined ECOM tasks.

Design rules (carried over from PAC1 — still apply to ECOM):
- The system prompt (`prompts.system_prompt()`) is kept bit-identical
  across runs for provider-side cache hits. Hints here are injected as
  an ADDITIONAL `role=user` message AFTER the task text in the agent
  loop, so the system prompt cache is preserved.
- Each matcher must be narrow. False positives on tasks where the
  agent was already correct risk regressing pass rate. We prefer a
  missed hint to a wrong hint.
- Each matcher is a pure function over `task_text`. No network, no
  filesystem.
- Matchers are ordered; the first matching hint wins and is returned.
- `hint_for_task` returns `None` when nothing applies — callers must
  handle that.
"""
from __future__ import annotations

from typing import Optional


def hint_for_task(task_text: str) -> Optional[str]:
    """Dispatch to whichever matcher (if any) applies to the task text.

    ECOM has no committed matchers yet. Add them above this docstring
    as failure clusters surface, then chain them here.
    """
    return None
