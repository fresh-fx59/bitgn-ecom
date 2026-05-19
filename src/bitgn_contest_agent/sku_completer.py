"""SKU completer — post-pass enforcer that ensures every qualifying
catalogue SKU is cited in grounding_refs on count tasks.

Target failure family (v0.1.74 / v0.1.81 PROD):
  t14: "How many of these products have at least N available …"
  t15: same shape, different product list
  t16: same shape, different brands

The agent's SQL workflow occasionally searches the wrong catalogue
partition for a multi-line product list and answers COUNT:K while
citing SKUs from the wrong category entirely. The grader then flags
`answer missing required reference <expected_sku_path>`.

This completer:
  1. Parses the task text into a list of ProductSpec (brand, line,
     attributes, kind name).
  2. Parses the store from the task (city descriptor or PowerTool
     shop name).
  3. For each ProductSpec, runs SQL against the catalogue
     (products + inventory) to find every SKU whose brand+series
     and attribute properties match the spec, with
     available_today >= the task's threshold at the named store.
  4. Adds any missing qualifying SKU path to grounding_refs.

Conservative on parsing failure: if any step can't resolve, the
completer abstains (refs unchanged). Only ADDS, never DROPS — the
SKU verifier handles overcitation drops.
"""
from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence


@dataclass
class ProductSpec:
    brand: str
    line_text: str        # "Acmetool Pro Z9 Z9-DR1 Cordless Drill Driver"
    name: str             # "Cordless Drill Driver"
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class CompleterResult:
    refs: list[str]
    added: list[str]
    reasons: list[str]
    aborted: bool = False
    abort_reason: str | None = None


# ── task parsing ─────────────────────────────────────────────────────


_THRESHOLD_RE = re.compile(
    r"at\s+least\s+(\d+)\s+items?\s+available", re.IGNORECASE
)


def parse_threshold(task_text: str) -> int | None:
    m = _THRESHOLD_RE.search(task_text)
    if not m:
        return None
    return int(m.group(1))


def _is_count_task(task_text: str) -> bool:
    """Heuristic gate: only fire on multi-product count tasks."""
    t = task_text.lower()
    if "how many of these products" not in t:
        return False
    if "at least" not in t:
        return False
    return True


_PRODUCT_RE = re.compile(
    r"the\s+(?P<name>[A-Z][A-Za-z\s/]+?)\s+from\s+(?P<brand>[A-Z][\w\s]+?)\s+"
    r"in\s+the\s+(?P<line>.+?)\s+line\s+that\s+has\s+(?P<attrs>.+?)"
    r"(?=,\s*the\b|,\s*and\s+the\b|\?\s*Answer|$)",
    re.IGNORECASE | re.DOTALL,
)


def parse_products(
    task_text: str, known_keys: set[str] | None = None
) -> list[ProductSpec]:
    """Extract product specs from the task text. ``known_keys`` is
    the set of attribute names defined in the catalogue's products
    table; pass it in to handle multi-word attribute names
    ("battery_platform" → "battery platform")."""
    out: list[ProductSpec] = []
    for m in _PRODUCT_RE.finditer(task_text):
        name = " ".join(m.group("name").split())
        brand = " ".join(m.group("brand").split())
        line = " ".join(m.group("line").split())
        attrs_text = m.group("attrs")
        attrs = _parse_attrs(attrs_text, known_keys=known_keys)
        out.append(
            ProductSpec(
                brand=brand,
                line_text=line,
                name=name,
                attributes=attrs,
            )
        )
    return out


def fetch_known_property_keys(
    run_sql: Callable[[str], str | None]
) -> set[str] | None:
    """Enumerate distinct attribute names defined in products.properties
    via SQLite's json_each. Returns None on SQL failure."""
    out = run_sql(
        "SELECT DISTINCT je.key FROM products p, "
        "json_each(p.properties) je;"
    )
    if out is None:
        return None
    body = _unwrap_sql(out)
    keys: set[str] = set()
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s in {"key", "key|"}:
            continue
        cols = _csv_split(s)
        if cols and cols[0]:
            keys.add(cols[0])
    return keys


# Property NAMES are 1-3 word lowercase tokens; VALUES can include
# digits, units, hyphens, commas inside lists, slashes, etc. The
# attribute list looks like:
#   "voltage 18 V, battery platform 18v-system, and kit contents case"
# Strategy: split on ", " and " and " then parse each "<key> <value>".
_ATTR_VALUE_TERMINATORS = re.compile(r"\s*(?:,\s*and\s+|,\s+|\s+and\s+)")


def _parse_attrs(
    attrs_text: str, known_keys: set[str] | None = None
) -> dict[str, str]:
    """Parse an attribute clause into a {key: value} dict.

    Attribute names can be multi-word in the catalogue
    (``battery_platform`` → "battery platform" in task text), so a
    naive greedy split fails. When ``known_keys`` is supplied
    (typically fetched from `SELECT DISTINCT json_each.key FROM
    products, json_each(properties)`), we longest-match-first
    against the known set; otherwise fall back to the single-word
    greedy heuristic.
    """
    out: dict[str, str] = {}
    parts = _ATTR_VALUE_TERMINATORS.split(attrs_text.strip().rstrip(","))
    known_spaces: dict[str, str] | None = None
    if known_keys:
        # Build "<key with spaces>" → "<key>" map sorted by length
        # so longer keys win (longest-prefix match).
        known_spaces = {
            k.replace("_", " "): k
            for k in known_keys
        }

    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Try longest-prefix match against known keys first.
        matched = False
        if known_spaces:
            lowered = part.lower()
            for ks in sorted(known_spaces, key=len, reverse=True):
                if lowered.startswith(ks + " "):
                    key = known_spaces[ks]
                    val = part[len(ks):].strip()
                    out[key] = val
                    matched = True
                    break
        if matched:
            continue
        # Fallback: single-word key, rest is value.
        m = re.match(r"^([a-z][a-z_]+?)\s+(.+)$", part)
        if not m:
            continue
        key = m.group(1).strip().replace(" ", "_")
        val = m.group(2).strip()
        out[key] = val
    return out


# ── store resolution ─────────────────────────────────────────────────


# Maps city-descriptor surface forms to store_id substrings the
# `stores.id` column uses. Matches /proc/stores/README.md (read in
# the pre-pass). When the task uses a literal store id ("store_X"),
# we pass through.
# Anchor on "available in <descriptor> ... today:" — the descriptor
# is between "available in" and a venue word (hardware|powertool|
# store|branch|shop), optionally followed by "today" and a colon.
_STORE_DESCRIPTOR_RE = re.compile(
    r"available\s+in\s+(?:the\s+)?"
    r"(?P<descriptor>[\w\s\-]+?)\s+"
    r"(?:hardware\s+(?:shop|store|branch)"
    r"|powertool\s+(?:shop|store|branch)"
    r"|store|branch|shop)"
    r"(?:\s+today)?\s*[:?,]",
    re.IGNORECASE,
)


def parse_store_descriptor(task_text: str) -> str | None:
    m = _STORE_DESCRIPTOR_RE.search(task_text)
    if m:
        return " ".join(m.group("descriptor").split())
    return None


# Map common city descriptors to lowercase tokens we expect to find
# in the store_id (e.g. "store_vienna_meidling"). The contest's
# /proc/stores/README.md is authoritative; we replicate the most
# common entries here so the completer doesn't need an extra read
# round-trip. Keys are normalized lowercase descriptor forms.
_CITY_TO_STORE_TOKENS: dict[str, list[str]] = {
    "central graz": ["graz_jakomini"],
    "north graz": ["graz_lend"],
    "graz lend": ["graz_lend"],
    "central vienna": ["vienna_praterstern"],
    "vienna praterstern": ["vienna_praterstern"],
    "vienna meidling": ["vienna_meidling"],
    "west-side vienna": ["vienna_meidling"],
    "old-town bratislava": ["bratislava_stare_mesto"],
    "central bratislava": ["bratislava_stare_mesto"],
    "bratislava stare mesto": ["bratislava_stare_mesto"],
    "main-square linz": ["linz_hauptplatz"],
    "central linz": ["linz_hauptplatz"],
    "linz hauptplatz": ["linz_hauptplatz"],
    "central salzburg": ["salzburg_elisabeth_vorstadt"],
    "near salzburg station": ["salzburg_elisabeth_vorstadt"],
    "salzburg elisabeth-vorstadt": ["salzburg_elisabeth_vorstadt"],
    "central innsbruck": ["innsbruck_wilten"],
    "innsbruck wilten": ["innsbruck_wilten"],
    "wilten": ["innsbruck_wilten"],
    "central brno": ["brno_veveri"],
    "brno veveri": ["brno_veveri"],
    "veveri": ["brno_veveri"],
    "downtown ljubljana": ["ljubljana_center"],
    "ljubljana center": ["ljubljana_center"],
}


def resolve_store_id(
    descriptor: str | None, run_sql: Callable[[str], str | None]
) -> str | None:
    """Look up a store_id from a city descriptor. Returns None on
    failure. The completer abstains rather than guess."""
    if not descriptor:
        return None
    norm = " ".join(descriptor.lower().split())
    # Strip "PowerTool" prefix and "shop"/"store" suffix variants.
    norm = re.sub(r"\bpowertool\b", "", norm).strip()
    norm = re.sub(r"\b(hardware|shop|store|branch)\b", "", norm).strip()
    norm = re.sub(r"\s+", " ", norm).strip()
    candidates = _CITY_TO_STORE_TOKENS.get(norm) or []
    # Fall back to the full descriptor as a substring.
    if not candidates:
        candidates = [norm.replace(" ", "_").replace("-", "_")]

    out = run_sql(
        "SELECT id FROM stores WHERE "
        + " OR ".join(
            f"id LIKE '%{tok}%'" for tok in candidates
        )
    )
    if not out:
        return None
    body = _unwrap_sql(out)
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s == "id" or s.startswith("id|"):
            continue
        # Could be CSV or pipe.
        sid = s.split(",")[0].split("|")[0].strip()
        if sid.startswith("store_"):
            return sid
    return None


# ── SQL helpers ──────────────────────────────────────────────────────


def _unwrap_sql(raw: str) -> str:
    """Identical wrapper as fraud_cluster_filter._unwrap_sql_output."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            obj = _json.loads(raw)
            if isinstance(obj, dict):
                return obj.get("stdout") or ""
        except Exception:
            pass
    return raw


def _csv_split(s: str) -> list[str]:
    """Split a CSV/PSV line, tolerant of either delimiter."""
    if "|" in s and "," not in s:
        return [c.strip() for c in s.split("|")]
    return [c.strip() for c in s.split(",")]


def _sql_quote(s: str) -> str:
    return s.replace("'", "''")


# ── per-spec SQL ─────────────────────────────────────────────────────


def _find_qualifying_skus(
    spec: ProductSpec,
    store_id: str,
    threshold: int,
    run_sql: Callable[[str], str | None],
) -> list[str] | None:
    """Return paths of catalogue SKUs that:
        (a) brand matches spec.brand exactly,
        (b) JSON properties contain every (key, value) in spec.attributes,
        (c) inventory.available_today >= threshold at store_id.
    Returns None on SQL failure (abstain at caller)."""
    brand_q = _sql_quote(spec.brand)
    where_attrs = []
    for k, v in spec.attributes.items():
        # SQLite json_extract on the properties TEXT column.
        v_q = _sql_quote(v)
        where_attrs.append(
            f"json_extract(p.properties, '$.{k}') = '{v_q}'"
        )
    attr_clause = (
        " AND " + " AND ".join(where_attrs) if where_attrs else ""
    )
    sql = (
        "SELECT p.path FROM products p "
        "JOIN inventory i ON i.sku = p.sku "
        f"WHERE p.brand = '{brand_q}' {attr_clause} "
        f"AND i.store_id = '{_sql_quote(store_id)}' "
        f"AND i.available_today >= {int(threshold)};"
    )
    out = run_sql(sql)
    if out is None:
        return None
    body = _unwrap_sql(out)
    paths: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s == "path" or s.startswith("path|"):
            continue
        cols = _csv_split(s)
        if cols and cols[0].startswith("/proc/catalog/"):
            paths.append(cols[0])
    return paths


# ── public API ───────────────────────────────────────────────────────


def complete_sku_refs(
    *,
    task_text: str,
    refs: Sequence[str],
    run_sql: Callable[[str], str | None],
) -> CompleterResult:
    """Ensure every qualifying SKU per the task spec is in
    grounding_refs. Returns the augmented refs + added list. Aborts
    silently (refs unchanged) on parse / SQL failures.
    """
    if not _is_count_task(task_text):
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="not a count task",
        )

    threshold = parse_threshold(task_text)
    if threshold is None:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="no threshold",
        )

    descriptor = parse_store_descriptor(task_text)
    store_id = resolve_store_id(descriptor, run_sql)
    if store_id is None:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason=f"store unresolved: {descriptor!r}",
        )

    known_keys = fetch_known_property_keys(run_sql)
    products = parse_products(task_text, known_keys=known_keys)
    if not products:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="no product specs parsed",
        )

    have = set(refs)
    out_refs = list(refs)
    added: list[str] = []
    reasons: list[str] = []

    for spec in products:
        skus = _find_qualifying_skus(spec, store_id, threshold, run_sql)
        if skus is None:
            return CompleterResult(
                refs=list(refs), added=[], reasons=[],
                aborted=True,
                abort_reason=f"sql failed for spec {spec.brand}",
            )
        for path in skus:
            if path not in have:
                out_refs.append(path)
                have.add(path)
                added.append(path)
                reasons.append(
                    f"{path}: qualifying SKU for {spec.brand} "
                    f"{spec.name} at {store_id} (available_today "
                    f">= {threshold}) was missing from grounding_refs"
                )
    return CompleterResult(
        refs=out_refs, added=added, reasons=reasons,
    )
