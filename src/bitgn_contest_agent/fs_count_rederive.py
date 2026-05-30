"""Filesystem re-derivation of count_per_store answers (PROD has no SQL).

PROD exposes no working /bin/sql (the SQL server is unreachable — ODBC
login-timeout), so the SQL-based count_rederive is dead there (memory
project_ecom_count_completer_dead_in_prod). On PROD, inventory is EMBEDDED
in the store record at /proc/locations/<City>/<store>.json and same-day
availability is max(on_hand - reserved, 0) per /docs/availability-checks.md
(memory project_ecom_prod_fs_ground_truth).

This module re-derives the count by reading those files directly, reusing
count_rederive's pure-Python matchers (_attr_matches, _extract_code) and
result/bounce types. Same contract as count_rederive: returns a count or
ABSTAIN (None); the CALLER bounces the terminal on disagreement. It NEVER
rewrites the agent's answer and abstains on ANY ambiguity.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

from bitgn_contest_agent.count_rederive import (  # reuse, don't fork
    RederiveResult,
    _attr_matches,
    _extract_code,
)
from bitgn_contest_agent.refless_count_override import _parse_threshold

# Descriptor stopwords (mirror sku_completer.resolve_store_id tokenisation).
_STORE_STOP = {
    "the", "shop", "store", "branch", "today", "near", "powertool",
    "hardware", "that", "has", "place", "tools", "and",
}


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_FS_REDERIVE_COUNT", "").strip() == "1"


# ── Shape B: raw-SKU-list count with compound on_hand/available/incoming ──
# The real PROD count_per_store tasks give an EXPLICIT SKU list + a compound
# predicate, and the classifier labels them kind="none" (so the task_spec
# path above never runs). This path parses the SKU list + predicate straight
# from the task TEXT — no classifier dependency, no fuzzy product resolution.
# Templates (verified on prod t005/t025/t045/t065):
#   B1: "...at least N units physically on hand, but fewer than M same-day
#        units available after reservations: <skus>?"  → on_hand>=N AND avail<M
#   B2: "...short of N same-day units, but would reach M units if incoming
#        stock due within D days is included: <skus>?" → avail<N AND
#        (avail + incoming_within_D) >= M
# available = max(on_hand - reserved, 0) per /docs/availability-checks.md.

_SHAPE_B_SIGNAL = re.compile(r"how many of these skus", re.I)
_SKU_TOKEN = re.compile(r"\b([A-Z0-9]{2,}(?:-[A-Z0-9]+)+)\b")
_B1_RE = re.compile(
    r"at least\s+(\d+)\s+units?\s+physically\s+on\s+hand.*?"
    r"fewer than\s+(\d+)\s+same-day\s+units?\s+available", re.I | re.S)
_B2_RE = re.compile(
    r"short of\s+(\d+)\s+same-day\s+units?.*?"
    r"would reach\s+(\d+)\s+units?\s+if\s+incoming\s+stock\s+due\s+within\s+(\d+)\s+days?",
    re.I | re.S)


def _parse_sku_list(text: str) -> list[str]:
    """SKU tokens from the list segment. The list ends at the '?'; the
    answer-format hint (which may contain a ':' as in '<COUNT:%d>') comes
    AFTER it, so cut on '?' first, then take the text after the last ':'."""
    seg = text.split("?", 1)[0]
    if ":" in seg:
        seg = seg.rsplit(":", 1)[1]
    return [m.group(1) for m in _SKU_TOKEN.finditer(seg)]


def _available(entry: dict) -> int:
    return max(int(entry.get("on_hand") or 0) - int(entry.get("reserved") or 0), 0)


def _incoming_within(entry: dict, days: int) -> int:
    tot = 0
    for inc in (entry.get("incoming") or []):
        try:
            if int(inc.get("arrival_in_days")) <= days:
                tot += int(inc.get("quantity") or 0)
        except (ValueError, TypeError):
            continue
    return tot


def _parse_shape_b_predicate(text: str):
    """Return a predicate over an inventory entry dict, or None (abstain)."""
    m = _B1_RE.search(text)
    if m:
        n_hand, m_avail = int(m.group(1)), int(m.group(2))
        return lambda e: int(e.get("on_hand") or 0) >= n_hand and _available(e) < m_avail
    m = _B2_RE.search(text)
    if m:
        n_short, m_reach, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return lambda e: _available(e) < n_short and (_available(e) + _incoming_within(e, d)) >= m_reach
    return None


def _all_store_files(list_fn) -> list[str]:
    out: list[str] = []
    for city in (list_fn("/proc/locations") or []):
        for f in (list_fn(city) or []):
            if f.endswith(".json"):
                out.append(f)
    return out


def _resolve_store_b(descriptor_text: str, list_fn, read_fn) -> Optional[dict]:
    """Resolve the named store to its record. Token-AND over descriptor
    tokens that match >=1 store id (dropping abbreviations that match none,
    e.g. 'ibk'), requiring a UNIQUE store. Abstains otherwise."""
    tokens = _desc_tokens(descriptor_text)
    if not tokens:
        return None
    paths = _all_store_files(list_fn)
    ids = {p.rsplit("/", 1)[-1][: -len(".json")].lower(): p for p in paths}
    eff = [t for t in tokens if any(t in sid for sid in ids)]
    if not eff:
        return None
    matches = [p for sid, p in ids.items() if all(t in sid for t in eff)]
    if len(matches) != 1:
        return None
    content = read_fn(matches[0])
    if not content:
        return None
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return None


def rederive_count_b(task_text: str, read_fn, list_fn) -> RederiveResult:
    """Re-derive a raw-SKU-list compound count straight from task text.
    Abstains on ANY ambiguity (unknown template, no SKUs, store not unique).
    The 'store descriptor' for resolution is taken as the text BEFORE the
    SKU list (the 'At <descriptor>, how many ...' clause)."""
    if not task_text or not _SHAPE_B_SIGNAL.search(task_text):
        return RederiveResult(None, reason="not shape-B")
    pred = _parse_shape_b_predicate(task_text)
    if pred is None:
        return RederiveResult(None, reason="shape-B predicate unparseable")
    skus = _parse_sku_list(task_text)
    if not skus:
        return RederiveResult(None, reason="no SKUs in list")
    # store descriptor = text up to "how many"
    head = re.split(r"how many", task_text, flags=re.I)[0]
    store = _resolve_store_b(head, list_fn, read_fn)
    if store is None:
        return RederiveResult(None, reason="store not uniquely resolved")
    inv = {e.get("sku"): e for e in (store.get("inventory") or []) if e.get("sku")}
    count = 0
    per: list = []
    for sku in skus:
        entry = inv.get(sku, {"on_hand": 0, "reserved": 0})  # absent → 0/0
        q = bool(pred(entry))
        per.append((sku, q))
        if q:
            count += 1
    return RederiveResult(count, per_product=per)


def build_bounce_reason_b(agent_n: int, rr: "RederiveResult") -> str:
    return (
        f"COUNT RE-DERIVATION (filesystem): you reported {agent_n} but an "
        f"independent re-derivation over the branch inventory yields {rr.count}. "
        f"Convention: same-day available = max(on_hand - reserved, 0); a SKU "
        f"absent from the branch inventory is 0 on both on_hand and available; "
        f"incoming counts only inside the stated due-within window. Re-evaluate "
        f"each listed SKU's on_hand/available against the compound condition. "
        f"Per-SKU verdicts (sku, qualifies): {rr.per_product}."
    )


def _desc_tokens(descriptor: str) -> list[str]:
    norm = " ".join((descriptor or "").lower().split())
    return [t for t in re.split(r"[^a-z0-9]+", norm)
            if len(t) >= 3 and t not in _STORE_STOP]


def _resolve_store(
    descriptor: str,
    list_fn: Callable[[str], list[str]],
    read_fn: Callable[[str], Optional[str]],
) -> Optional[tuple[str, dict]]:
    """Token-AND match the descriptor against store ids under
    /proc/locations/<City>/<id>.json (mirrors resolve_store_id). Returns
    (store_id, store_obj) only on a UNIQUE match, else None (abstain)."""
    tokens = _desc_tokens(descriptor)
    if not tokens:
        return None
    store_paths: list[str] = []
    for city in (list_fn("/proc/locations") or []):
        for f in (list_fn(city) or []):
            if f.endswith(".json"):
                store_paths.append(f)
    matches: list[tuple[str, str]] = []
    for p in store_paths:
        sid = p.rsplit("/", 1)[-1][: -len(".json")]
        low = sid.lower()
        if all(t in low for t in tokens):
            matches.append((sid, p))
    if len(matches) != 1:
        return None
    sid, path = matches[0]
    content = read_fn(path)
    if not content:
        return None
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return None
    return sid, obj


def _inventory_map(store_obj: dict) -> dict[str, int]:
    """{sku: same-day available} = max(on_hand - reserved, 0) per the PROD
    availability-checks rule. SKU absent → treated as 0 by the caller."""
    inv: dict[str, int] = {}
    for e in (store_obj.get("inventory") or []):
        sku = e.get("sku")
        if not sku:
            continue
        try:
            oh = int(e.get("on_hand") or 0)
            rs = int(e.get("reserved") or 0)
        except (ValueError, TypeError):
            continue
        inv[sku] = max(oh - rs, 0)
    return inv


def _props_dict(catalog_obj: dict) -> dict:
    """Shape props as count_rederive's _attr_matches expects:
    {key_lower: (value_text_lower, value_number_or_'')}."""
    out: dict = {}
    for k, v in (catalog_obj.get("properties") or {}).items():
        m = re.search(r"(\d+(?:\.\d+)?)", str(v))
        out[str(k).lower()] = (str(v).lower(), m.group(1) if m else "")
    return out


def _uniq(paths) -> list[str]:
    seen, out = set(), []
    for p in paths or []:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def rederive_count_fs(
    task_spec,
    read_fn: Callable[[str], Optional[str]],
    search_fn: Callable[[str, str], list[str]],
    list_fn: Callable[[str], list[str]],
    task_text: str,
) -> RederiveResult:
    """Re-derive count_per_store from the PROD filesystem. Candidate set
    from /proc/catalog (brand + line code + attrs) BEFORE inventory; the
    direction-aware threshold (from _parse_threshold) is applied to the
    embedded store availability (max(on_hand-reserved,0)), a SKU absent
    from the store inventory counting as 0. Abstains on ANY ambiguity."""
    if task_spec is None:
        return RederiveResult(None, reason="no task_spec")
    if getattr(task_spec, "kind", "none") != "count_per_store":
        return RederiveResult(None, reason=f"kind={getattr(task_spec, 'kind', 'none')!r}")

    pred = _parse_threshold(task_text)
    if pred is None:
        return RederiveResult(None, reason="threshold/direction unparseable")

    products = list(getattr(task_spec, "products", None) or [])
    if not products:
        return RederiveResult(None, reason="no products")

    resolved = _resolve_store(getattr(task_spec, "store_descriptor", "") or "", list_fn, read_fn)
    if resolved is None:
        return RederiveResult(None, reason="store not uniquely resolved")
    _store_id, store_obj = resolved
    inv = _inventory_map(store_obj)

    count = 0
    per_product: list = []
    for p in products:
        brand = getattr(p, "brand", "") or ""
        if not brand:
            return RederiveResult(None, reason="product missing brand")
        code = _extract_code(p)
        if not code:
            return RederiveResult(None, reason=f"no code for brand {brand}")
        attrs = dict(getattr(p, "attributes", {}) or {})

        # candidate variants on this LINE: catalog records containing the
        # line code, filtered to the brand, then attrs matched in Python.
        cand_paths = _uniq(search_fn("/proc/catalog", re.escape(code)))
        matched: list[str] = []
        for cp in cand_paths:
            c = read_fn(cp)
            if not c:
                continue
            try:
                obj = json.loads(c)
            except (ValueError, TypeError):
                continue
            if (obj.get("brand", "") or "").lower() != brand.lower():
                continue
            if all(_attr_matches(k, v, _props_dict(obj)) for k, v in attrs.items()):
                sku = obj.get("sku")
                if sku:
                    matched.append(sku)
        if not matched:
            return RederiveResult(None, reason=f"attrs resolve no variant: {brand} {code}")

        sides = {bool(pred(inv.get(sku, 0))) for sku in matched}
        if len(sides) != 1:
            return RederiveResult(None, reason=f"matched variants disagree: {brand} {code}")
        qualifies = sides == {True}
        per_product.append((brand, code, qualifies))
        if qualifies:
            count += 1

    return RederiveResult(count, per_product=per_product)
