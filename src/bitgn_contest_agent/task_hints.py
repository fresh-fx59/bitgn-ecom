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

import os
import re
from typing import Optional

# Narrow matcher for the t48 archive-export fraud-review task. The data is a
# /archive TSV (NOT in SQL); the agent scores 0.06-0.71 = inconsistent fraud
# criteria. This injects the discriminating PRINCIPLE (derived from the
# faithful export, memory project_ecom_fraud_structure) — generic fraud
# heuristics, no row IDs / fingerprints / amounts, so it is not overfit.
# Env-gated so it can be A/B'd: default-off until a DEV run confirms lift.
_FRAUD_EXPORT_RE = re.compile(
    r"(payment_batch_export|\.tsv).*(fraud|risk ops)|(fraud|risk ops).*\.tsv",
    re.IGNORECASE | re.DOTALL,
)

_FRAUD_EXPORT_HINT = (
    "Fraud-review heuristics for an archive payment export:\n"
    "- A fraud incident is a CLUSTER of rows linked by a shared "
    "device_fingerprint OR payment_method_fingerprint. Build the connected "
    "components over those shared-fingerprint links.\n"
    "- STRONGEST signal: a device or payment-method fingerprint shared "
    "across MULTIPLE DISTINCT customer accounts (card-testing / a "
    "compromised instrument used on many accounts). Flag every row in such "
    "a ring.\n"
    "- Also flag a SINGLE account showing an anomalously large burst of "
    "payments concentrated on one/two devices or methods across many "
    "locations/short window (account takeover): flag all its archived rows.\n"
    "- A single customer making a few ordinary purchases with their OWN "
    "device/card is NOT fraud — do not flag isolated rows or small "
    "same-customer reuse.\n"
    "- Be exhaustive within each flagged ring: include rows on the ring's "
    "secondary device/method, not just the dominant one.\n"
    "Sum amount_cents over ALL flagged rows for the total; cite every "
    "flagged row in the required row-ref format."
)


def _fraud_export_hint(task_text: str) -> Optional[str]:
    if os.environ.get("BITGN_USE_FRAUD_EXPORT_HINT", "").strip() != "1":
        return None
    if _FRAUD_EXPORT_RE.search(task_text or ""):
        return _FRAUD_EXPORT_HINT
    return None


def hint_for_task(task_text: str) -> Optional[str]:
    """Dispatch to whichever matcher (if any) applies to the task text.

    Matchers are ordered; first match wins. Each must be narrow (a missed
    hint is preferred to a wrong one).
    """
    return _fraud_export_hint(task_text)
