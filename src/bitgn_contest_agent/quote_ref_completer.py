"""Ref completer for pasted-list / quote tasks (t47 family).

These tasks paste a list of product descriptions and ask for a per-row
TSV (``RowID\\tSKU\\tin_stock\\tmatch``), and the rulebook requires
"reference all matched SKUs in response". The observed failure mode is
UNDER-MATCHING: the agent declares a genuinely-exact catalogue product
"no exact match", leaves SKU/in_stock empty, and omits its grounding ref
(see memory ``project_ecom_t47_tsv_oracle``). e.g. on t47 the agent cited
only 1 of 4 exact-match SKUs → grader "missing required reference".

This completer resolves each pasted row to its exact catalogue variant
(line code + ALL stated attributes, exact — the same engine as the refless
count override) and UNIONS the matched SKUs' record_paths into
grounding_refs. It is ADD-ONLY (pure superset, never removes a ref and
never touches the message), which is the safe enforcer pattern endorsed by
memory ``feedback_enforcer_cannot_replace_adaptive_llm``: it cannot make a
correct answer wrong, only supply refs the agent under-cited. Per-row it
abstains (adds nothing for that row) on any ambiguity.

Env-gated default-off via ``BITGN_USE_QUOTE_REF_COMPLETER``.
"""

from __future__ import annotations

import os
import re
from typing import Callable

from bitgn_contest_agent.refless_count_override import (
    _CODE_RE,
    _phrase_satisfied,
)
from bitgn_contest_agent.sku_completer import _csv_split, _unwrap_sql


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_QUOTE_REF_COMPLETER", "").strip() == "1"


# Markers that identify the pasted-list / quote task family.
_QUOTE_MARKERS = (
    "pasted product list",
    "rowid",
    "tab-separated output",
    "check each row against",
)


def looks_like_quote_task(task_text: str) -> bool:
    t = (task_text or "").lower()
    hits = sum(1 for m in _QUOTE_MARKERS if m in t)
    return hits >= 2


def _fetch_props(run_sql: Callable[[str], str | None], sku: str) -> dict[str, str]:
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
        text_v = cols[1] if len(cols) > 1 else ""
        num_v = cols[2] if len(cols) > 2 else ""
        val = text_v if text_v not in ("", "None", "null") else num_v
        if val not in ("", "None", "null"):
            props[cols[0].lower()] = str(val).lower()
    return props


def resolve_matched_refs(
    run_sql: Callable[[str], str | None], task_text: str
) -> list[str]:
    """Return record_paths of every pasted row that resolves to exactly one
    exact-match catalogue variant. Rows that are ambiguous or have no exact
    match contribute nothing (the agent's call stands for those)."""
    if not task_text:
        return []
    # Each row description carries a "NNN-NNN" model code + "that has <attrs>".
    # Split the pasted block into per-row descriptions on the line codes.
    codes = _CODE_RE.findall(task_text)
    if not codes:
        return []
    refs: list[str] = []
    for code in codes:
        # the description fragment around this code, up to the next code or EOL
        idx = task_text.find(code)
        frag = task_text[idx: idx + 400]
        if "that has" not in frag:
            continue
        attrs_txt = frag.split("that has", 1)[1]
        # cut at the requested-quantity tab/newline boundary
        attrs_txt = re.split(r"\t|\n|\r", attrs_txt)[0]
        phrases = [
            p.strip()
            for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", attrs_txt)
            if p.strip()
        ]
        if not phrases:
            continue
        out = run_sql(
            "SELECT product_sku, record_path FROM product_variants "
            f"WHERE model LIKE '%{code}%';"
        )
        if out is None:
            continue
        cands: list[tuple[str, str]] = []
        for line in _unwrap_sql(out).splitlines():
            s = line.strip()
            if not s or s.startswith("[") or s.lower().startswith("product_sku"):
                continue
            cols = _csv_split(s)
            if len(cols) >= 2 and cols[0]:
                cands.append((cols[0], cols[1]))
        matched = [
            (sku, path)
            for sku, path in cands
            if all(_phrase_satisfied(ph, _fetch_props(run_sql, sku)) for ph in phrases)
        ]
        if len(matched) == 1:
            refs.append(matched[0][1])
    return refs


def complete_quote_refs(
    run_sql: Callable[[str], str | None],
    task_text: str,
    existing_refs: list[str],
) -> list[str]:
    """Return matched-SKU record_paths NOT already in existing_refs."""
    if not looks_like_quote_task(task_text):
        return []
    have = set(existing_refs or [])
    return [r for r in resolve_matched_refs(run_sql, task_text) if r not in have]
