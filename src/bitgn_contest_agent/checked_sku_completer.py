"""Last-resort `Checked SKU:` back-completer.

Mitigates the yes_no_sku recall gap captured in
project_yes_no_sku_recall_gap.md (saved memory): the agent emits
`<NO> Checked SKU: ELC-1SPATM3U …` in the terminal message but
ships `grounding_refs=[]`. Prompt rule E
(`src/bitgn_contest_agent/prompts.py:365`) already mandates citing
the closest-miss product file, but the LLM under-cites under the
precision pressure from rule B's worked anti-example.

Why this exists as a separate module (not folded into
`sku_completer.complete_yes_no_sku_refs`):
  - The legacy yes_no_sku completer relies on SQL to enumerate the
    brand+series family. PROD has SQL so it usually works. The
    cases that bleed through are the ones where the LLM picked the
    wrong SQL strategy or SQL returned nothing — local snapshots
    expose this because they ship without an SQL backend.
  - This module operates entirely on the agent's own evidence: the
    message body (what the agent claims to have checked) and
    `seen_refs` (what it actually read). No SQL, no LLM, no
    network. Fast, deterministic, and safe to run after every
    enforcer in the chain.

Safety guards (NEVER violated):
  1. NEVER rewrites the LLM's `<YES>/<NO>` token or message body.
  2. NEVER adds a ref that is not already in `seen_refs`.
  3. NEVER fires when the LLM has cited any meaningful ref already
     (anything beyond `/AGENTS.MD`).
  4. Adds at most one ref per call — the SKU the agent explicitly
     named with `Checked SKU:` framing.

Env gating:
  BITGN_USE_CHECKED_SKU_COMPLETER=1 → fire
  (default unset)                  → skip; legacy chain unchanged
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass


# Examples that the contest log shows in PROD/local:
#   "Checked SKU: ELC-1SPATM3U"
#   "Checked SKU: WRC-12345AB"
# Be liberal about the separator and case but anchor to the
# explicit "Checked SKU" framing so we don't sweep up SKUs
# mentioned in passing in long messages.
_CHECKED_SKU_RE = re.compile(
    r"\bChecked\s+SKUs?\s*[:\-]?\s*([A-Z]{2,4}-[A-Z0-9]{4,16})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CheckedSkuResult:
    refs: list[str]
    added: list[str]
    fired: bool
    sku_found: str = ""
    reason: str = ""


def _is_meaningful_ref(r: str) -> bool:
    """True for refs that count as the agent actually citing
    something. /AGENTS.MD is required boilerplate, not evidence."""
    return r.strip().upper() != "/AGENTS.MD"


def enabled() -> bool:
    """Read the env gate. Default: off."""
    return os.environ.get(
        "BITGN_USE_CHECKED_SKU_COMPLETER", ""
    ).strip().lower() in ("1", "true", "yes")


def complete_checked_sku_refs(
    *,
    kind: str,
    message: str,
    refs: list[str],
    seen_refs: Iterable[str],
) -> CheckedSkuResult:
    """Return a CheckedSkuResult. `fired=True` iff a ref was added.

    See module docstring for the safety guards. This function is
    deterministic — same input always yields same output, no LLM
    or network calls."""
    base = list(refs)
    if kind != "yes_no_sku":
        return CheckedSkuResult(
            refs=base, added=[], fired=False, reason="kind_mismatch",
        )
    if any(_is_meaningful_ref(r) for r in base):
        return CheckedSkuResult(
            refs=base, added=[], fired=False, reason="already_cited",
        )
    m = _CHECKED_SKU_RE.search(message or "")
    if not m:
        return CheckedSkuResult(
            refs=base, added=[], fired=False, reason="no_checked_sku_marker",
        )
    sku = m.group(1).upper()
    needle = f"/{sku}.JSON"
    seen_set = set(seen_refs)
    # Match anywhere in the path so we hit nested family directories.
    # Use endswith so we don't accidentally match a substring of a
    # longer SKU.
    for path in sorted(seen_set):
        if path.upper().endswith(needle):
            return CheckedSkuResult(
                refs=base + [path],
                added=[path],
                fired=True,
                sku_found=sku,
                reason="added_from_seen_refs",
            )
    return CheckedSkuResult(
        refs=base, added=[], fired=False,
        sku_found=sku, reason="sku_not_in_seen_refs",
    )
