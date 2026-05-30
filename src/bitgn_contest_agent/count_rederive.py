"""Deterministic in-trial re-derivation of count_per_store answers.

Independent path: candidate set from product_variants(+properties) BEFORE
inventory; LEFT JOIN + COALESCE(...,0) (missing row = 0 available); threshold
applied direction-aware. Returns a count or ABSTAIN (None) + per-product
verdicts. The CALLER bounces the terminal on disagreement; this module never
rewrites and abstains on ambiguity. See docs/SPEC_RELIABILITY_53.md.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from bitgn_contest_agent.refless_count_override import _parse_threshold
from bitgn_contest_agent.sku_completer import (
    _detect_schema,
    _sql_quote,
    resolve_store_id,
)

# Catalogue SKU-line code pattern (e.g. "1CD-A3X"). The LLM's parse may place
# this code in ProductFilter.model OR embed it in .series / .name, so we search
# all three fields before falling back to the last token of .model. This is the
# reliability goal: be robust to per-world parse variation.
_CODE_RE = re.compile(r"\b([0-9A-Z]{3}-[0-9A-Z]{3})\b")


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_REDERIVE_COUNT", "").strip() == "1"


@dataclass
class RederiveResult:
    count: Optional[int]  # None = ABSTAIN (no bounce)
    per_product: list = field(default_factory=list)
    reason: str = ""

    @property
    def abstained(self) -> bool:
        return self.count is None


def _split(line: str) -> list[str]:
    s = line.strip()
    return [c.strip() for c in (s.split("|") if "|" in s else s.split(","))]


def _rows(run_sql, sql: str) -> list[list[str]]:
    out = run_sql(sql)
    if out is None:
        return []
    res = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s or s.startswith("["):
            continue
        res.append(_split(s))
    return res


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9.]", "", str(v).lower())


def _attr_matches(named_key: str, named_val: str, props: dict) -> bool:
    """props: {prop_key_lower: (value_text_lower_or_raw, value_number_or_'')}."""
    nk_lead = re.split(r"[_\s]", named_key.lower())[0]
    nv = _norm(named_val)
    m = re.search(r"(\d+(?:\.\d+)?)", named_val)
    named_num = float(m.group(1)) if m else None
    for pkey, (ptext, pnum) in props.items():
        if re.split(r"[_\s]", pkey)[0] != nk_lead and nk_lead not in pkey:
            continue
        if nv and (nv in _norm(ptext) or (_norm(ptext) and _norm(ptext) in nv)):
            return True
        if named_num is not None:
            for cand in (pnum, ptext):
                tm = re.search(r"(\d+(?:\.\d+)?)", str(cand))
                if tm and abs(float(tm.group(1)) - named_num) < 1e-9:
                    return True
    return False


def _extract_code(product) -> str:
    """Robustly extract the catalogue SKU-line code (e.g. "1CD-A3X").

    The LLM's structured parse places the code inconsistently across worlds:
    sometimes in ``.model``, sometimes embedded in ``.series`` or ``.name``.
    Search all three for the code pattern, then fall back to the last token of
    ``.model`` only if no pattern matches anywhere.
    """
    model = getattr(product, "model", "") or ""
    series = getattr(product, "series", "") or ""
    name = getattr(product, "name", "") or ""
    for field_val in (model, series, name):
        m = _CODE_RE.search(field_val)
        if m:
            return m.group(1)
    return model.split()[-1] if model.split() else ""


def rederive_count(task_spec, run_sql, task_text) -> RederiveResult:
    """Re-derive the count_per_store answer through an independent SQL path.

    Candidate set from product_variants(+properties) BEFORE inventory; the
    threshold predicate (direction-aware, from ``_parse_threshold``) is applied
    to ``COALESCE(available_today_quantity, 0)`` so a missing inventory row =
    0 available. Abstains (count=None) on ANY ambiguity — never guesses, never
    rewrites.
    """
    if task_spec is None:
        return RederiveResult(None, reason="no task_spec")
    if getattr(task_spec, "kind", "none") != "count_per_store":
        return RederiveResult(
            None, reason=f"kind={getattr(task_spec, 'kind', 'none')!r}"
        )

    pred = _parse_threshold(task_text)
    if pred is None:
        return RederiveResult(None, reason="threshold/direction unparseable")

    products = list(getattr(task_spec, "products", None) or [])
    if not products:
        return RederiveResult(None, reason="no products")

    # Confirm a known schema is present (PROD product_variants or legacy).
    if _detect_schema(run_sql) is None:
        return RederiveResult(None, reason="unknown schema")

    store_id = resolve_store_id(getattr(task_spec, "store_descriptor", "") or "", run_sql)
    if store_id is None:
        return RederiveResult(None, reason="store not uniquely resolved")

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

        # Candidate variants on this product LINE (brand + code in model or
        # name). No attribute filter in SQL — attrs are matched loosely in
        # Python via _attr_matches (e.g. 500 ≠ 5000), then verified exactly.
        family = _rows(
            run_sql,
            "SELECT product_sku FROM product_variants WHERE "
            f"brand='{_sql_quote(brand)}' COLLATE NOCASE AND "
            f"(model LIKE '%{_sql_quote(code)}%' OR "
            f"product_name LIKE '%{_sql_quote(code)}%');",
        )
        family_skus = [r[0] for r in family if r and r[0] and not r[0].lower().startswith("product_sku")]
        if not family_skus:
            return RederiveResult(None, reason=f"line unresolved: {brand} {code}")

        matched: list[str] = []
        for sku in family_skus:
            prop_rows = _rows(
                run_sql,
                "SELECT property_key, property_value_text, property_value_number "
                f"FROM product_variant_properties WHERE product_sku='{_sql_quote(sku)}';",
            )
            props: dict = {}
            for r in prop_rows:
                if not r or not r[0] or r[0].lower() == "property_key":
                    continue
                key = r[0].lower()
                text_v = r[1] if len(r) > 1 else ""
                num_v = r[2] if len(r) > 2 else ""
                props[key] = (str(text_v).lower(), num_v)
            if all(_attr_matches(k, v, props) for k, v in attrs.items()):
                matched.append(sku)

        if not matched:
            return RederiveResult(None, reason=f"attrs resolve no variant: {brand} {code}")

        # available_today at store_id; missing inventory row → 0 (LEFT JOIN +
        # COALESCE). Threshold side computed per matched variant.
        sides = set()
        for sku in matched:
            avail_rows = _rows(
                run_sql,
                "SELECT COALESCE(i.available_today_quantity, 0) FROM product_variants p "
                "LEFT JOIN store_inventory i ON i.product_sku = p.product_sku AND "
                f"i.store_id = '{_sql_quote(store_id)}' "
                f"WHERE p.product_sku = '{_sql_quote(sku)}';",
            )
            avail = 0
            for r in avail_rows:
                if not r or not r[0] or r[0].lower().startswith("coalesce"):
                    continue
                cell = r[-1]
                try:
                    avail = int(float(cell))
                except (ValueError, TypeError):
                    avail = 0
                break
            sides.add(bool(pred(avail)))

        # Matched variants disagreeing on the threshold side → genuine
        # ambiguity → abstain.
        if len(sides) != 1:
            return RederiveResult(None, reason=f"matched variants disagree: {brand} {code}")
        qualifies = sides == {True}
        per_product.append((brand, code, qualifies))
        if qualifies:
            count += 1

    return RederiveResult(count, per_product=per_product)
