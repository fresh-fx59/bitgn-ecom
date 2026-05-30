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
