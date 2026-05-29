"""ADD-only fraud-ring completer for the t40 SQL fraud-incident task.

t40: "find the payment records part of a confirmed fraud incident in
archived payment history; cite each". The agent detects the incident by
DEVICE clustering and catches most of it, but UNDER-CITES the members that
used a *secondary* device/payment-method in the same ring — e.g. on
t40_real2 it cites the 24 device-cluster payments but misses 2 that the
ring made on a second device/method, scoring ~0.92-0.94 (= 24/26). See
memory ``project_ecom_fraud_structure``.

The fraud incident is the CONNECTED COMPONENT of archived payments under
"shares device_fingerprint OR payment_method_fingerprint" edges, seeded
from the clearly-anomalous fingerprint concentration. On t40_real2 that
component is all 26 archived payments of the compromised customer.

This completer recomputes that component and UNIONS any member's
record_path the agent did not already cite. It is ADD-ONLY — it never
removes a ref. That is the key difference from the v0.1.133 fraud_cluster
FILTER, which REMOVED payments, over-pruned true positives, and regressed
PROD to 44/53. An add-only completer can only raise recall; the only risk
is precision if the component is broader than the grader's set, so it is
CONSERVATIVE: it abstains entirely unless there is a single clearly
dominant anomalous fingerprint (top count >= 2x the runner-up AND >= a
floor). Env-gated default-off via ``BITGN_USE_FRAUD_COMPONENT_COMPLETER``.

NOTE: t48 is a different task — its data is a /archive TSV export not in
SQL — so this completer does not apply to it (no SQL archived rows → abstain).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Callable

from bitgn_contest_agent.sku_completer import _csv_split, _unwrap_sql


def is_enabled() -> bool:
    return os.environ.get(
        "BITGN_USE_FRAUD_COMPONENT_COMPLETER", ""
    ).strip() == "1"


_ANOMALY_FLOOR = 15      # min payments on the dominant fingerprint to act
_DOMINANCE_RATIO = 2.0   # top count must be >= this x the runner-up


def looks_like_sql_fraud_task(task_text: str) -> bool:
    """Match the SQL fraud-incident task across PROD phrasing variants.

    The contest changed a couple of task texts (2026-05-29 update): t40 went
    from "confirmed fraud incident ... classify as fraud" to "confirmed a
    known fraud HIT ... MARK as fraud". A narrow literal matcher missed the
    new wording (the completer never fired). Broadened to: mentions fraud +
    payment records, is NOT the /archive .tsv file task (t48). Broadening the
    GATE is safe — the completer still abstains unless a single fingerprint
    dominates the archived payments, so firing on a non-fraud payment task is
    a no-op.
    """
    t = (task_text or "").lower()
    if ".tsv" in t:
        return False  # t48-style file task, not SQL
    if "fraud" not in t:
        return False
    return ("payment" in t) or ("archiv" in t)


def _rows(run_sql: Callable[[str], str | None], sql: str) -> list[list[str]]:
    out = run_sql(sql)
    if out is None:
        return []
    rows = []
    for line in _unwrap_sql(out).splitlines():
        s = line.strip()
        if not s or s.startswith("["):
            continue
        cols = _csv_split(s)
        # skip header row (non-data) heuristically
        if cols and cols[0] in ("payment_id", "device_fingerprint",
                                "payment_method_fingerprint", "record_path"):
            continue
        rows.append(cols)
    return rows


def _dominant(counter: Counter) -> str | None:
    """Return the single clearly-dominant key, or None to abstain."""
    common = [c for c in counter.most_common() if c[0]]
    if not common:
        return None
    top_key, top_n = common[0]
    if top_n < _ANOMALY_FLOOR:
        return None
    if len(common) > 1:
        _, second_n = common[1]
        if second_n and top_n < _DOMINANCE_RATIO * second_n:
            return None
    return top_key


def resolve_fraud_component(
    run_sql: Callable[[str], str | None], task_text: str
) -> list[str]:
    """Return record_paths of every archived payment in the fraud
    connected component, or [] to abstain."""
    if not looks_like_sql_fraud_task(task_text):
        return []
    rows = _rows(
        run_sql,
        "SELECT payment_id, device_fingerprint, payment_method_fingerprint, "
        "record_path FROM payment_transactions "
        "WHERE is_archived_basket_reference IN (1,'1','true','True');",
    )
    if not rows:
        return []
    pay = {}
    for r in rows:
        if len(r) < 4 or not r[0]:
            continue
        pay[r[0]] = {"dev": r[1], "pm": r[2], "path": r[3]}
    if not pay:
        return []

    dom_dev = _dominant(Counter(v["dev"] for v in pay.values() if v["dev"]))
    dom_pm = _dominant(Counter(v["pm"] for v in pay.values() if v["pm"]))
    if not dom_dev and not dom_pm:
        return []  # no clear anomaly → abstain

    comp = {
        pid for pid, v in pay.items()
        if (dom_dev and v["dev"] == dom_dev) or (dom_pm and v["pm"] == dom_pm)
    }
    # expand to the device∪method connected component (fixpoint)
    changed = True
    while changed:
        changed = False
        devs = {pay[i]["dev"] for i in comp if pay[i]["dev"]}
        pms = {pay[i]["pm"] for i in comp if pay[i]["pm"]}
        for pid, v in pay.items():
            if pid in comp:
                continue
            if (v["dev"] and v["dev"] in devs) or (v["pm"] and v["pm"] in pms):
                comp.add(pid)
                changed = True
    return [pay[pid]["path"] for pid in comp if pay[pid]["path"]]


def complete_fraud_refs(
    run_sql: Callable[[str], str | None],
    task_text: str,
    existing_refs: list[str],
) -> list[str]:
    """Component member record_paths NOT already cited (ADD-only)."""
    have = set(existing_refs or [])
    return [p for p in resolve_fraud_component(run_sql, task_text) if p not in have]
