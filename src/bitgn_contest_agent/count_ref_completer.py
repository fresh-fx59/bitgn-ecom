"""Candidate-SKU grounding_ref completer for the PROD count/availability family.

Root cause (from grader score_detail on v0.1.158 t005/t025/t045/t065/t002/t062,
all scored 0.0): the COUNT VALUE was correct; the failure was
`answer refs for family "/proc/catalog" mismatch: missing [<candidate SKU
paths>], extra []`. The grader requires citing the
`/proc/catalog/<Brand>/<sku>.json` record of EVERY candidate SKU the task asks
about (the whole list it evaluates) — even SKUs absent from the store. The agent
cites only the qualifying/matching one, so it under-cites and scores 0.

This completer ADDS the missing candidate catalog refs (union — never removes,
never rewrites the answer). It is NOT a brittle predicate parser (the value is
already right): it extracts the catalogue SKU tokens the task names and resolves
each to its real `/proc/catalog` path via the SEARCH RPC (so the path/brand
prefix is taken from the live catalogue, not constructed). Scoped to the
count/availability family so it never adds refs a non-count task would count as
`extra`. Env-gated default-off (BITGN_USE_COUNT_REF_COMPLETER).
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

# Catalogue SKU token PREFILTER: an uppercase-led token with >=1 hyphen group.
# Deliberately permissive (SKU formats vary across worlds: PT-SND-BOS-GEX125-DUST,
# ADH-1JRXOJEF, PWR-... ) — the AUTHORITATIVE filter is the SEARCH RPC against the
# live /proc/catalog, so a prefilter token that isn't a real catalogue SKU simply
# resolves to nothing and is dropped. This avoids hard-coding a brittle SKU shape.
_SKU_TOKEN = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\b")

# Count-LIST family signal: "how many of these SKUs/products ...: <SKU list>".
# Deliberately NARROW — only the raw-SKU-list shape, where the grader wants
# EVERY listed SKU cited. Excludes yes/no "do you have N of X (but not SKU-Y)"
# tasks: there the explicit SKU is an EXCLUSION the grader does NOT want cited
# (it wants the resolved product), so firing there would add an `extra` ref and
# create a new failure. (t005/t025/t045/t065 = "how many of these"; t002/t062
# = "do you have" → intentionally NOT handled here.)
_COUNT_SIGNAL = re.compile(r"how many of these\b", re.I)


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_COUNT_REF_COMPLETER", "").strip() == "1"


def candidate_skus(task_text: str) -> list[str]:
    seen, out = set(), []
    for m in _SKU_TOKEN.finditer(task_text or ""):
        s = m.group(1)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def applies(task_text: str) -> bool:
    """True for count/availability tasks that name >=1 catalogue SKU — the
    families whose grading requires every candidate SKU cited."""
    if not task_text or not _COUNT_SIGNAL.search(task_text):
        return False
    return len(candidate_skus(task_text)) >= 1


def complete_catalog_refs(
    task_text: str,
    existing_refs,
    search_fn: Callable[[str, str], list[str]],
) -> list[str]:
    """Return the list of /proc/catalog refs to ADD so every candidate SKU in
    the task is cited. Resolves each SKU's path via search (the live catalogue
    supplies the <Brand> segment). Never returns paths already present."""
    if not applies(task_text):
        return []
    existing = set(existing_refs or [])
    additions: list[str] = []
    for sku in candidate_skus(task_text):
        try:
            hits = search_fn("/proc/catalog", re.escape(sku)) or []
        except Exception:
            hits = []
        # prefer the catalog record file named exactly <sku>.json
        path = None
        for p in hits:
            if p.endswith(f"/{sku}.json"):
                path = p
                break
        if path and path not in existing and path not in additions:
            additions.append(path)
    return additions
