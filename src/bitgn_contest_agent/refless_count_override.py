"""Deterministic count for REFLESS ``count_per_store`` tasks.

These tasks have the canonical shape::

    How many of these products have <THRESHOLD> items available in the
    <STORE> today: the <Product> from <Brand> in the <series> <CODE>
    <Product> line that has <attr>[, <attr>][, and <attr>], the <...>?
    Answer in exactly format "<COUNT:%d>" (or "result %d").

They are *refless*: the grader scores the single integer answer, and no
grounding references are required. That property is exactly what makes a
deterministic override SAFE here where the v0.1.139 override regressed:

  * v0.1.139 fired on count tasks that ALSO carry required grounding refs,
    so an override count that disagreed with the (separately added) cited
    SKUs broke count-cite parity (t14). A refless task has no refs to
    disagree with, so that failure mode is structurally impossible.

The count of qualifying products is *observable and deterministic* — we
replicate the grader's own computation against the same catalogue DB
(unlike fraud, whose seeded ground-truth set is unobservable and where a
deterministic enforcer over-prunes; see memory
``project_ecom_count_completer_dead_in_prod``).

SAFETY CONTRACT: this resolver returns ``None`` (ABSTAIN) on *any*
ambiguity — unparseable threshold/direction, store not uniquely resolved,
or any product line that does not resolve to a single qualifying side of
the threshold. Abstaining is a no-op (the agent's own answer stands), so
the worst case is the current behaviour, never a confidently-wrong
override. The caller only rewrites the message integer when this returns a
concrete int.

Env-gated default-off via ``BITGN_USE_REFLESS_COUNT_OVERRIDE``.
"""

from __future__ import annotations

import os
import re
from typing import Callable

from bitgn_contest_agent.sku_completer import _csv_split, _unwrap_sql


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_REFLESS_COUNT_OVERRIDE", "").strip() == "1"


def _norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# Text detector for the refless count template, used to fire the override
# even when the LLM classifier leaves task_spec.kind unset (observed in PROD:
# kind=None on a clear count_per_store task → the kind-gated override was
# dead). The resolver still abstains on any ambiguity, so a loose detector is
# safe — worst case it calls the resolver which returns None.
_COUNT_TEMPLATE_RE = re.compile(
    r"how many of these products .* available .* in ", re.IGNORECASE | re.DOTALL
)


def looks_like_refless_count(task_text: str) -> bool:
    if not task_text:
        return False
    if not _COUNT_TEMPLATE_RE.search(task_text):
        return False
    # must also carry a parseable threshold direction (else resolver abstains)
    return _parse_threshold(task_text) is not None


# ── threshold direction ──────────────────────────────────────────────
# Exactly one comparison must be present, else we cannot know the
# direction and must abstain.
_THRESHOLD_PATTERNS = [
    (r"fewer than (\d+)", lambda n: (lambda a: a < n)),
    (r"less than (\d+)", lambda n: (lambda a: a < n)),
    (r"at least (\d+)", lambda n: (lambda a: a >= n)),
    (r"at most (\d+)", lambda n: (lambda a: a <= n)),
    (r"more than (\d+)", lambda n: (lambda a: a > n)),
    (r"(\d+) or more", lambda n: (lambda a: a >= n)),
    (r"(\d+) or fewer", lambda n: (lambda a: a <= n)),
]


def _parse_threshold(text: str) -> Callable[[int], bool] | None:
    t = text.lower()
    hits = []
    for rx, make in _THRESHOLD_PATTERNS:
        m = re.search(rx, t)
        if m:
            hits.append((int(m.group(1)), make))
    if len(hits) != 1:
        return None
    n, make = hits[0]
    return make(n)


# ── store resolution ─────────────────────────────────────────────────
# Two observed phrasings: "the <Area> PowerTool {shop|store} in <City>"
# and free-form descriptors. We only handle the structured phrasing and
# abstain otherwise; the store must resolve to exactly one row.
_STORE_RE = re.compile(
    r"the ([a-z]+) powertool (?:shop|store) in ([a-z]+)", re.IGNORECASE
)


def _resolve_store(run_sql: Callable[[str], str | None], text: str) -> str | None:
    m = _STORE_RE.search(text)
    if not m:
        return None
    area, city = m.group(1), m.group(2)
    out = run_sql("SELECT store_id, store_name, city FROM stores;")
    if out is None:
        return None
    matches = []
    for line in _unwrap_sql(out).splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s.lower().startswith("store_id"):
            continue
        cols = _csv_split(s)
        if len(cols) < 3:
            continue
        sid, sname, scity = cols[0], cols[1], cols[2]
        hay_area = _norm(sid) + _norm(sname)
        hay_city = _norm(scity) + _norm(sname)
        if _norm(area) in hay_area and _norm(city) in hay_city:
            matches.append(sid)
    return matches[0] if len(matches) == 1 else None


# ── per-product attribute resolution ─────────────────────────────────
_CODE_RE = re.compile(r"\b([0-9A-Z]{3}-[0-9A-Z]{3})\b")
_NUMUNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|l|mm|cm|m|w|v|a|gsm|nm)\b", re.I)


def _phrase_satisfied(phrase: str, props: dict[str, str]) -> bool:
    """True iff some catalogue property matches this "<key> <value>" phrase.

    Key match: the phrase must contain the leading token of the property
    key (e.g. "color family Orange" → key ``color_family`` leads with
    "color"). Value match: textual containment, or — when the phrase has a
    numeric+unit value — EXACT numeric equality (so "volume 500 ml" does
    not match ``volume_ml=5000``).
    """
    pnorm = _norm(phrase)
    num_m = _NUMUNIT_RE.search(phrase)
    for key, val in props.items():
        if val is None:
            continue
        lead = re.split(r"[_\s]", key.lower())[0]
        if not lead or _norm(lead) not in pnorm:
            continue
        nval = _norm(val)
        if nval and nval in pnorm:
            return True
        if num_m:
            try:
                want = float(num_m.group(1))
                have = float(re.sub(r"[^0-9.]", "", val))
                if abs(want - have) < 1e-9:
                    return True
            except (ValueError, TypeError):
                pass
    return False


def _fetch_props(
    run_sql: Callable[[str], str | None], sku: str
) -> dict[str, str]:
    out = run_sql(
        "SELECT property_key, property_value_text, property_value_number "
        f"FROM product_variant_properties WHERE product_sku='{sku}';"
    )
    props: dict[str, str] = {}
    if out is None:
        return props
    for line in _unwrap_sql(out).splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s.lower().startswith("property_key"):
            continue
        cols = _csv_split(s)
        if len(cols) < 2:
            continue
        key = cols[0].lower()
        text_v = cols[1] if len(cols) > 1 else ""
        num_v = cols[2] if len(cols) > 2 else ""
        val = text_v if text_v not in ("", "None", "null") else num_v
        if val not in ("", "None", "null"):
            props[key] = str(val).lower()
    return props


def _available(
    run_sql: Callable[[str], str | None], store_id: str, sku: str
) -> int:
    """available_today_quantity at the store; absent inventory row → 0."""
    out = run_sql(
        "SELECT available_today_quantity FROM store_inventory "
        f"WHERE store_id='{store_id}' AND product_sku='{sku}';"
    )
    if out is None:
        return 0
    for line in _unwrap_sql(out).splitlines():
        s = line.strip()
        if not s or s.startswith("[") or "available" in s.lower():
            continue
        cell = _csv_split(s)[0]
        try:
            return int(float(cell))
        except (ValueError, TypeError):
            return 0
    return 0


def compute_refless_count_from_spec(
    task_spec, run_sql: Callable[[str], str | None], task_text: str
) -> int | None:
    """Spec-based refless count — the PROD-robust path.

    Consumes the LLM's ADAPTIVE structured parse (``task_spec.products`` +
    ``store_descriptor``) instead of re-parsing the raw task text. This
    fixes the v145 text-parser's PROD failure: it abstained because store/
    attribute PHRASING VARIES PER WORLD ("Vienna Meidling hardware branch" ≠
    the rigid "the X PowerTool shop in Y" regex). The LLM already normalised
    those into task_spec, and ``resolve_store_id`` (token-based) handles the
    varying descriptors. Deterministic SQL then counts qualifying products
    with the direction parsed from task_text (reliable: "fewer than"/"at
    least"). Refless + count-token-only (no ref parity risk). Abstains on any
    ambiguity. See memory project_ecom_count_completer_dead_in_prod.
    """
    if task_spec is None:
        return None
    pred = _parse_threshold(task_text)
    if pred is None:
        return None
    products = getattr(task_spec, "products", None) or []
    if not products:
        return None
    try:
        from bitgn_contest_agent.sku_completer import (
            _detect_schema,
            _sql_quote,
            resolve_store_id,
        )
    except Exception:
        return None
    sch = _detect_schema(run_sql)
    if sch is None:
        return None
    store_id = resolve_store_id(
        getattr(task_spec, "store_descriptor", "") or "", run_sql
    )
    if store_id is None:
        return None

    count = 0
    for p in products:
        brand = getattr(p, "brand", "") or ""
        model = getattr(p, "model", "") or ""
        series = getattr(p, "series", "") or ""
        attrs = dict(getattr(p, "attributes", {}) or {})
        if not brand:
            return None
        model_code = model.split()[-1] if model else ""
        line_filter = ""
        if model_code:
            line_filter = f" AND p.model LIKE '%{_sql_quote(model_code)}%'"
        elif series:
            line_filter = f" AND p.series LIKE '%{_sql_quote(series)}%'"
        # Candidate variants on this line at the store (NO attr filter in
        # SQL — _attr_filter matches loosely, e.g. 500≈5000; we match attrs
        # EXACTLY in Python via _phrase_satisfied below). LEFT JOIN so a
        # variant with no inventory row → 0 (out of stock); 0 candidate rows
        # → line unresolvable → abstain.
        sql = (
            f"SELECT p.\"{sch['sku']}\", COALESCE(i.{sch['inv_avail']}, 0) "
            f"FROM {sch['tbl']} p "
            f"LEFT JOIN {sch['inv_tbl']} i ON i.{sch['inv_sku']} = "
            f"p.\"{sch['sku']}\" AND i.{sch['inv_store']} = "
            f"'{_sql_quote(store_id)}' "
            f"WHERE p.brand = '{_sql_quote(brand)}' COLLATE NOCASE"
            f"{line_filter} LIMIT 50;"
        )
        out = run_sql(sql)
        if out is None:
            return None
        avail_by_sku: dict[str, int] = {}
        for ln in _unwrap_sql_local(out).splitlines():
            s = ln.strip()
            if not s or s.startswith("[") or s.lower().startswith(sch["sku"].lower()):
                continue
            cols = [c.strip() for c in (s.split("|") if "|" in s else s.split(","))]
            if not cols or not cols[0]:
                continue
            av = cols[-1]
            avail_by_sku[cols[0]] = int(av) if av.lstrip("-").isdigit() else 0
        if not avail_by_sku:
            return None  # line unresolvable → abstain
        # EXACT attribute match in Python (reuse _phrase_satisfied, which does
        # exact-numeric + textual containment, so "500 ml" ≠ volume_ml 5000).
        propmap = _fetch_props_bulk(run_sql, sch, list(avail_by_sku))
        matched = [
            sku for sku in avail_by_sku
            if all(
                _phrase_satisfied(f"{k} {v}", propmap.get(sku, {}))
                for k, v in attrs.items()
            )
        ]
        if not matched:
            return None  # attributes resolve no variant → abstain
        sides = {bool(pred(avail_by_sku[sku])) for sku in matched}
        if len(sides) != 1:
            return None  # matched variants disagree on threshold side → abstain
        if sides == {True}:
            count += 1
    return count


def _fetch_props_bulk(run_sql, sch, skus):
    """{sku: {prop_key_lower: value_lower}} for the given SKUs (PROD schema)."""
    from bitgn_contest_agent.sku_completer import _sql_quote
    if not skus:
        return {}
    in_list = ",".join(f"'{_sql_quote(s)}'" for s in skus)
    out = run_sql(
        f"SELECT {sch['props_sku']}, {sch['props_key']}, "
        f"property_value_text, property_value_number "
        f"FROM {sch['props_tbl']} WHERE {sch['props_sku']} IN ({in_list});"
    )
    props: dict[str, dict[str, str]] = {}
    if out is None:
        return props
    for ln in _unwrap_sql_local(out).splitlines():
        s = ln.strip()
        if not s or s.startswith("[") or s.lower().startswith(sch["props_sku"].lower()):
            continue
        cols = [c.strip() for c in (s.split("|") if "|" in s else s.split(","))]
        if len(cols) < 2:
            continue
        sku, key = cols[0], cols[1].lower()
        text_v = cols[2] if len(cols) > 2 else ""
        num_v = cols[3] if len(cols) > 3 else ""
        val = text_v if text_v not in ("", "None", "null") else num_v
        if val not in ("", "None", "null"):
            props.setdefault(sku, {})[key] = str(val).lower()
    return props


def _unwrap_sql_local(out: str) -> str:
    from bitgn_contest_agent.sku_completer import _unwrap_sql as _u
    return _u(out)


def compute_refless_count(
    run_sql: Callable[[str], str | None], task_text: str
) -> int | None:
    """Return the qualifying-product count, or ``None`` to abstain.

    Abstains on any ambiguity (see module SAFETY CONTRACT).
    """
    if not task_text:
        return None
    pred = _parse_threshold(task_text)
    if pred is None:
        return None
    store_id = _resolve_store(run_sql, task_text)
    if store_id is None:
        return None

    codes = _CODE_RE.findall(task_text)
    if not codes:
        return None
    segments = re.split(r",\s*the ", task_text)

    count = 0
    for code in codes:
        seg = next((s for s in segments if code in s), "")
        if "that has" not in seg:
            return None  # cannot read attrs → abstain
        attrs_txt = seg.split("that has", 1)[1]
        phrases = [
            re.sub(r"\?.*$", "", p).strip()
            for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", attrs_txt)
        ]
        phrases = [p for p in phrases if p and p != "?"]
        if not phrases:
            return None

        out = run_sql(
            "SELECT product_sku, model FROM product_variants "
            f"WHERE model LIKE '%{code}%';"
        )
        if out is None:
            return None
        skus = []
        for line in _unwrap_sql(out).splitlines():
            s = line.strip()
            if not s or s.startswith("[") or s.lower().startswith("product_sku"):
                continue
            cols = _csv_split(s)
            if cols and cols[0]:
                skus.append(cols[0])
        if not skus:
            return None

        qualifying_sides = set()
        matched_skus = []
        for sku in skus:
            props = _fetch_props(run_sql, sku)
            if all(_phrase_satisfied(ph, props) for ph in phrases):
                matched_skus.append(sku)
        if not matched_skus:
            return None
        for sku in matched_skus:
            avail = _available(run_sql, store_id, sku)
            qualifying_sides.add(bool(pred(avail)))
        # If multiple variants match the (under-specified) attrs but they
        # disagree on which side of the threshold they fall, the count is
        # genuinely ambiguous → abstain. If they agree, the count is well
        # defined regardless of which exact SKU was intended.
        if len(qualifying_sides) != 1:
            return None
        if qualifying_sides == {True}:
            count += 1
    return count
