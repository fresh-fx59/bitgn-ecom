"""Refund payment back-completer — chains a cited return record to
its linked payment record.

Mitigates a recall gap captured from PROD bench
v0.1.117-pre run-22Rhf9YSdfBmJjNbA4hhncGmr (2026-05-28):

  t44 instruction: "Approve the customer refund tied to return ret_007."
  Agent reads ret_007.json → status='requested' → emits
    OUTCOME_NONE_UNSUPPORTED with grounding_refs that include
    /proc/returns/ret_007.json but NOT /proc/payments/pay_013.json.
  Grader: "answer missing required reference '/proc/payments/pay_013.json'".

The return record carries a `payment_id` field linking it to a
payment row. The agent often never reads that payment record
because the refusal decision is reached entirely from the return's
status. The legacy `cite_completer` only injects POLICY docs
(`/docs/security.md`, `/docs/returns.md`) and obeys the
seen_refs-only safety gate, so it can't bring in the payment ref.

This completer:
  1. Detects refund family by task-text or task_spec_kind.
  2. For every `/proc/returns/ret_NNN.json` already in
     `grounding_refs`, opens the file (via `read_cache` first,
     adapter `Req_Read` fallback).
  3. Parses the JSON, reads `payment_id`.
  4. Adds `/proc/payments/<payment_id>.json` to grounding_refs.

Safety guards (NEVER violated):
  - NEVER adds a payment ref that the agent did not implicitly
    reference through a return record it already cited (the return
    is the agent's own commitment).
  - NEVER rewrites the LLM's message body or outcome token.
  - Reads at most one payment ref per cited return (no
    overcollection).
  - Aborts cleanly when the return JSON is missing/unparseable —
    `cite_completer` rationale still applies, no silent injection
    of unverified paths.

Env gating:
  BITGN_USE_REFUND_PAYMENT_COMPLETER=1 → fire
  (default unset)                       → skip
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Optional


_LOG = logging.getLogger(__name__)


# task-text fingerprints for refund family — kept in lockstep with
# cite_completer's refund detection.
_REFUND_RE = re.compile(
    r"\b(refund|approve[ -]refund|return(?:ed|s|ing)?|reimburs)\w*",
    re.IGNORECASE,
)

_RETURN_REF_RE = re.compile(r"^/proc/returns/ret_\d+\.json$")
_PAYMENT_ID_RE = re.compile(r"^pay_\d+$")


@dataclass(frozen=True)
class RefundPaymentResult:
    refs: list[str]
    added: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""


def enabled() -> bool:
    return os.environ.get(
        "BITGN_USE_REFUND_PAYMENT_COMPLETER", ""
    ).strip().lower() in ("1", "true", "yes")


def _is_refund(task_text: str, kind: str) -> bool:
    if kind and kind.startswith("refund"):
        return True
    return bool(_REFUND_RE.search(task_text or ""))


def _extract_payment_id(body: str | None) -> Optional[str]:
    if not body:
        return None
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    pid = obj.get("payment_id") or obj.get("pay_id")
    if isinstance(pid, str) and _PAYMENT_ID_RE.match(pid):
        return pid
    return None


def complete_refund_payment_refs(
    *,
    task_text: str,
    kind: str,
    refs: list[str],
    read_cache: Mapping[str, str] | None,
    read: Callable[[str], str | None] | None = None,
) -> RefundPaymentResult:
    """Add payment refs linked to cited returns.

    `read_cache` is the agent loop's per-trial body cache (key: path,
    value: inner content). `read` is an optional fallback that
    fetches a body via the adapter when the cache misses; if None,
    only cached returns are followed (safer, no extra RPC).
    """
    if not _is_refund(task_text, kind):
        return RefundPaymentResult(
            refs=list(refs), aborted=True, abort_reason="not_refund_family",
        )
    return_refs = [r for r in refs if _RETURN_REF_RE.match(r)]
    if not return_refs:
        return RefundPaymentResult(
            refs=list(refs), aborted=True, abort_reason="no_return_ref",
        )

    base = list(refs)
    added: list[str] = []
    skipped: list[tuple[str, str]] = []
    cache = read_cache or {}

    for ret_path in return_refs:
        body = cache.get(ret_path)
        if body is None and read is not None:
            try:
                body = read(ret_path)
            except Exception as exc:  # pragma: no cover — fail soft
                _LOG.info(
                    "refund_payment_completer: read failed %s: %s",
                    ret_path, exc,
                )
                skipped.append((ret_path, f"read_failed:{exc}"))
                continue
        pid = _extract_payment_id(body)
        if not pid:
            skipped.append((ret_path, "no_payment_id"))
            continue
        pay_ref = f"/proc/payments/{pid}.json"
        if pay_ref in base or pay_ref in added:
            skipped.append((ret_path, "already_cited"))
            continue
        added.append(pay_ref)

    return RefundPaymentResult(
        refs=base + added,
        added=added,
        skipped=skipped,
        aborted=False,
    )
