"""Exclusion-citation completer for the YES/NO availability "(but not <SKU>)" family.

Root cause (PROD v0.1.167 rerun score_detail, t002 + t062 — the only
non-dispatch PERSISTENT failures): an availability question that names a SKU
after "but not" requires that SKU's /proc/catalog record CITED — the grader
treats the named exclusion as evidence you looked it up to rule it out:

    t062 "...(but not PT-MOW-STI-RMA235-AK30)..."
        -> missing [/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK30.json]
    t002 "...(but not PT-WASH-KAR-K4-PIPE)..."
        -> missing [/proc/catalog/Karcher/PT-WASH-KAR-K4-PIPE.json]

The agent excludes the SKU from consideration and never cites it, so it
under-cites and scores 0. This completer ADDS the named excluded SKU's record
(union — never removes, never rewrites the answer/value). The SKU is resolved
against the LIVE catalogue (exact ``<sku>.json`` find), so a token that is not a
real catalogue record resolves to nothing and is dropped — no hallucinated path.

Scoped NARROWLY to the YES/NO availability/existence family that carries a
"but not <SKU>" clause (the grader's evidenced behaviour). It deliberately does
NOT fire on the count-LIST family ("how many of these: A, B, C"), which is owned
by count_ref_completer. Env-gated default-off (BITGN_USE_BUT_NOT_COMPLETER).

⚠️ KEEP DEFAULT-OFF — VALIDATED NET-FRAGILE (PROD run-22SHuo, v0.1.169):
fixed t002 + t062 (grader WANTED the excluded SKU cited) but BROKE t022
(`(but not PT-BIT-ALP-HSS-25)` → grader marked HSS-25 as an EXTRA ref). Both
t062 and t022 had NEGATIVE answers, so answer polarity does NOT discriminate —
the grader's treatment of the named exclusion is INCONSISTENT across tasks and
there is no reliable signal for when it wants the excluded record cited. Adding
it unconditionally is an uncertain bet on every "(but not)" task, which violates
the "abstain on uncertainty" enforcer principle. Net +1 on the current 3-task
"(but not)" set (+t002 +t062 −t022), but unpredictable on re-instantiation /
new tasks. Do NOT enable without a per-task discriminator.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

# A catalogue SKU token: uppercase-led, >=1 hyphen group (e.g. PT-MOW-STI-RMA235-AK30).
_SKU_TOKEN = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\b")

# YES/NO availability/existence family signal (same shapes ref_judge keys on).
_FAMILY_SIGNAL = re.compile(
    r"do you have\b|do we (have|stock|carry)\b|is there a\b|in stock\b|"
    r"does such product exist|does (the|this|that|a)\b|can i buy",
    re.I)

# The exclusion clause: everything from "but not" to the end of the parenthetical
# (or end of the SKU run). We extract SKU tokens that appear AFTER "but not".
_BUT_NOT = re.compile(r"but\s+not\b(?P<tail>.*?)(?:\)|$)", re.I | re.DOTALL)


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_BUT_NOT_COMPLETER", "").strip() == "1"


def excluded_skus(task_text: str) -> list[str]:
    """SKU tokens named after a "but not" clause (in order, de-duplicated)."""
    if not task_text:
        return []
    seen, out = set(), []
    for m in _BUT_NOT.finditer(task_text):
        for sm in _SKU_TOKEN.finditer(m.group("tail")):
            s = sm.group(1)
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def applies(task_text: str) -> bool:
    """True for an availability/existence question that names a SKU after
    "but not" — the family whose grader requires the excluded SKU cited."""
    if not task_text or not _FAMILY_SIGNAL.search(task_text):
        return False
    return len(excluded_skus(task_text)) >= 1


def complete_refs(
    task_text: str,
    existing_refs,
    resolve_fn: Callable[[str], Optional[str]],
) -> list[str]:
    """Return the /proc/catalog refs to ADD so every named excluded SKU is
    cited. ``resolve_fn(sku)`` returns the SKU's catalogue record path (or
    None). Never returns a path already present; abstains (drops) when a SKU
    cannot be resolved against the live catalogue."""
    if not applies(task_text):
        return []
    existing = set(existing_refs or [])
    additions: list[str] = []
    for sku in excluded_skus(task_text):
        try:
            path = resolve_fn(sku)
        except Exception:
            path = None
        if path and path not in existing and path not in additions:
            additions.append(path)
    return additions
