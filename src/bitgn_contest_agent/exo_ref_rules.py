"""Three deterministic grounding-ref rules ported from the muxx/exoskeleton
submission_refs.py (1st-place ECOM1 Live PROD solution). Grounding refs are graded
as an EXACT set with low per-instance variance, so deterministic ref hygiene is
high-ROI. Each rule is a pure function with dependency-injected runtime access
(testable without a live VM) and is independently env-gated.

  1. crosslist_refs (BITGN_USE_CROSSLIST_REF_FILTER) — a "create a TSV crosslist
     report ... cite the upload OCR path" task must cite ONLY the /uploads/ OCR
     ref; the agent over-cites catalogue/export paths → grader "too many invalid
     references" (t076).
  2. discount_actor_emp_refs (BITGN_USE_DISCOUNT_EMP_REF) — a discount task issued
     by an employee actor must cite the actor's own /proc/staff|employees record
     (proof of authority); the agent forgets it (t099 "missing emp record").
  3. drop_nonexistent_refs (BITGN_USE_REF_STAT_GUARD) — drop any /proc record ref
     that a runtime stat reports as definitively missing (hallucinated/constructed
     path); conservative: keeps seen refs, docs, archive #row refs, and anything
     whose existence is unknown/transient.

See docs/superpowers/plans/2026-06-02-exoskeleton-competitor-analysis.md.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

_UPLOADS_RE = re.compile(r"/uploads/\S+", re.IGNORECASE)
_EMP_ID_RE = re.compile(r"^emp[-_]\d+$", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)\]]+$")


# ── flags ─────────────────────────────────────────────────────────────────
def crosslist_is_enabled() -> bool:
    return os.environ.get("BITGN_USE_CROSSLIST_REF_FILTER", "").strip() == "1"


def discount_emp_is_enabled() -> bool:
    return os.environ.get("BITGN_USE_DISCOUNT_EMP_REF", "").strip() == "1"


def stat_guard_is_enabled() -> bool:
    return os.environ.get("BITGN_USE_REF_STAT_GUARD", "").strip() == "1"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ── Rule 1: crosslist ref filter ───────────────────────────────────────────
def is_crosslist_task(task_text: str) -> bool:
    t = (task_text or "").lower()
    return "crosslist" in t and "/uploads/" in t


def _upload_paths_in(text: str) -> list[str]:
    return [_TRAILING_PUNCT_RE.sub("", m.group(0)) for m in _UPLOADS_RE.finditer(text or "")]


def crosslist_refs(task_text: str, refs: list[str]) -> Optional[list[str]]:
    """For a crosslist/export task, return the refs reduced to ONLY the /uploads/
    OCR path(s) (task-named ∪ already-cited). ``None`` to abstain (not a crosslist
    task, or no upload path resolvable)."""
    if not is_crosslist_task(task_text):
        return None
    keep = _dedupe(
        _upload_paths_in(task_text)
        + [r for r in (refs or []) if "/uploads/" in r.lower()]
    )
    return keep or None


# ── Rule 2: discount → issuer-employee ref ─────────────────────────────────
def is_discount_actor_task(task_text: str) -> bool:
    return "discount" in (task_text or "").lower()


def _is_employee_actor(actor_id: Optional[str]) -> bool:
    return bool(actor_id) and bool(_EMP_ID_RE.match(actor_id))


def discount_actor_emp_refs(
    task_text: str,
    actor_id: Optional[str],
    resolve_fn: Callable[[str], Optional[str]],
) -> list[str]:
    """For a discount task issued by an employee actor, return [the actor's own
    /proc/staff|employees record path] to ADD (union). Empty list to add nothing
    (not a discount task, non-employee actor, or unresolvable)."""
    if not is_discount_actor_task(task_text) or not _is_employee_actor(actor_id):
        return []
    try:
        path = resolve_fn(actor_id)  # type: ignore[arg-type]
    except Exception:
        return []
    return [path] if path else []


# ── Rule 3: stat-check, drop non-existent refs ─────────────────────────────
def drop_nonexistent_refs(
    refs: list[str],
    seen_refs: set[str],
    exists_fn: Callable[[str], Optional[bool]],
) -> list[str]:
    """Drop /proc record refs that ``exists_fn`` reports as definitively missing
    (returns False). Conservative — keeps a ref when it was already read
    (``seen_refs``), is a document (.md), is an /archive #row fragment, is not a
    /proc record, or when existence is unknown/transient (``exists_fn`` returns
    None). Never calls ``exists_fn`` for a seen ref."""
    out: list[str] = []
    seen = seen_refs or set()
    for ref in refs or []:
        if "#" in ref:  # archive row-ref / fragment — cannot stat a fragment
            out.append(ref)
            continue
        if ref in seen:
            out.append(ref)
            continue
        path = ref
        if path in seen:
            out.append(ref)
            continue
        if path.endswith(".md"):
            out.append(ref)
            continue
        if not path.startswith("/proc/"):
            out.append(ref)
            continue
        if exists_fn(path) is False:
            continue  # definitively missing → drop
        out.append(ref)
    return out
