"""Store back-completer — chains a cited employee record to the
actor's home-store record.

Mitigates a recall gap captured from PROD bench
v0.1.117-pre run-22Rhf9YSdfBmJjNbA4hhncGmr (2026-05-28):

  t47 instruction begins "I'm preparing a quote for a customer
    from this pasted product list. Check each row against our
    exact catalogue and my store's same-day availability."
  Agent reads /proc/employees/emp_036.json → resolves
    store_id=store_bratislava_stare_mesto → goes directly to
    SQL inventory queries against that store → emits the
    requested table.
  grounding_refs includes /proc/employees/emp_036.json and the
    matched SKUs, but NOT /proc/stores/store_bratislava_stare_mesto.json.
  Grader: "answer missing required reference
    '/proc/stores/store_bratislava_stare_mesto.json'".

The employee record's `store_id` is the store the answer is
about — same logic as `refund_payment_completer` chaining the
return's `payment_id`. Without an explicit read, the agent
never lands the store JSON in seen_refs.

This completer:
  1. Detects store-availability tasks via text fingerprints
     ("my store", "this store", "same-day", "in stock",
     "available today", etc.) — keeps the trigger generic so it
     covers quote/availability/inventory phrasings.
  2. For every `/proc/employees/emp_NNN.json` already in
     `grounding_refs`, opens the record and extracts `store_id`.
  3. Adds `/proc/stores/<store_id>.json` to grounding_refs.

Safety guards (NEVER violated):
  - NEVER adds a store ref unless the grounding chain includes
    an employee record the agent itself cited.
  - NEVER rewrites the LLM's message body or outcome token.
  - Aborts cleanly when the employee JSON is missing/unparseable.

Env gating:
  BITGN_USE_STORE_BACK_COMPLETER=1 → fire
  (default unset)                    → skip
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Optional


_LOG = logging.getLogger(__name__)


# Match store-availability vocabulary. Keep broad: quote tasks,
# availability tasks, inventory pulls, store-specific aggregations.
_STORE_AVAILABILITY_RE = re.compile(
    r"\b("
    r"my\s+store|your\s+store|this\s+store|the\s+store|"
    r"same[\- ]day\s+(availability|inventory|stock)|"
    r"available\s+today|"
    r"in\s+stock|"
    r"available\s+in\s+stock|"
    r"store\W?s\s+(same|stock|inventory|availability)"
    r")\b",
    re.IGNORECASE,
)

_EMP_REF_RE = re.compile(r"^/proc/employees/emp_\d+\.json$")
_STORE_ID_RE = re.compile(r"^store_[a-z0-9_]+$")


@dataclass(frozen=True)
class StoreBackResult:
    refs: list[str]
    added: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""


def enabled() -> bool:
    return os.environ.get(
        "BITGN_USE_STORE_BACK_COMPLETER", ""
    ).strip().lower() in ("1", "true", "yes")


def _is_store_availability_task(task_text: str) -> bool:
    return bool(_STORE_AVAILABILITY_RE.search(task_text or ""))


def _extract_store_id(body: str | None) -> Optional[str]:
    if not body:
        return None
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    sid = obj.get("store_id") or obj.get("home_store_id")
    if isinstance(sid, str) and _STORE_ID_RE.match(sid):
        return sid
    return None


def complete_store_back_refs(
    *,
    task_text: str,
    refs: list[str],
    read_cache: Mapping[str, str] | None,
    read: Callable[[str], str | None] | None = None,
    actor_id: str | None = None,
) -> StoreBackResult:
    """Add the actor's home-store ref when the task names
    store-specific availability AND we can resolve an employee
    record (either already cited, or via the prepass actor identity).
    """
    if not _is_store_availability_task(task_text):
        return StoreBackResult(
            refs=list(refs), aborted=True,
            abort_reason="not_store_availability_task",
        )
    emp_refs = [r for r in refs if _EMP_REF_RE.match(r)]
    # v0.1.117-pre+: fall back to prepass actor identity (e.g. emp_036
    # from /bin/id) when the agent took a SQL-only path and never
    # cited the employee record. The completer reads emp_<actor>.json
    # via the read callback below to extract store_id.
    if not emp_refs and actor_id and actor_id.startswith("emp_"):
        emp_refs = [f"/proc/employees/{actor_id}.json"]
    if not emp_refs:
        return StoreBackResult(
            refs=list(refs), aborted=True,
            abort_reason="no_employee_ref",
        )

    base = list(refs)
    added: list[str] = []
    skipped: list[tuple[str, str]] = []
    cache = read_cache or {}

    for emp_path in emp_refs:
        body = cache.get(emp_path)
        if body is None and read is not None:
            try:
                body = read(emp_path)
            except Exception as exc:  # pragma: no cover
                _LOG.info(
                    "store_back_completer: read failed %s: %s",
                    emp_path, exc,
                )
                skipped.append((emp_path, f"read_failed:{exc}"))
                continue
        sid = _extract_store_id(body)
        if not sid:
            skipped.append((emp_path, "no_store_id"))
            continue
        store_ref = f"/proc/stores/{sid}.json"
        if store_ref in base or store_ref in added:
            skipped.append((emp_path, "already_cited"))
            continue
        added.append(store_ref)

    return StoreBackResult(
        refs=base + added,
        added=added,
        skipped=skipped,
        aborted=False,
    )
