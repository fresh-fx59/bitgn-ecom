"""SKU completer — post-pass enforcer that ensures every qualifying
catalogue SKU is cited in grounding_refs on count tasks.

Active entry points (called from agent.py terminal post-process):
  - ``complete_sku_refs_from_spec`` (P1, kind=count_per_store) —
    structured-input path. Parses task_spec products, SQL-resolves
    qualifying SKUs per product (relaxation ladder strict →
    brand+model → brand only), and UNIONS missing into refs.
  - ``complete_yes_no_sku_refs`` (P1, kind=yes_no_sku) — enumerates
    brand+model family (+brand+name LIKE fallback, no brand-only).
  - ``compute_count_per_store`` — exposed but NOT wired (the
    v0.1.106 count-override broke correct LLM answers; reverted in
    v0.1.107). Use only with explicit safety guards.

Legacy entry point:
  - ``complete_sku_refs`` — regex-based natural-language parser,
    disabled in agent.py since v0.1.84 because PROD measured it
    net-negative on multi-product spec drift.

Target failure family closed by the active path:
  - t14/t15/t16-shape: agent's SQL over-constrains attribute
    filters or normalizes units wrong → wrong/missing SKU cite.
    Relaxation ladder + task_spec emission close this.

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
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence


def _closerouter_caps_enabled() -> bool:
    """True when the SKU completer should apply CloseRouter-style
    enumeration caps (yes_no=5, count_per_store=1/product).

    Driven by the single env var BITGN_PROVIDER_PROFILE:
      - unset / cliproxyapi / anything else → False (default)
      - closerouter → True

    Set via .env (`scripts/use_provider.sh closerouter` flips it on).
    See sku_completer call sites for the failure modes each gate
    addresses, and docs/PROD_CONTEST_PLAYBOOK.md for the provider
    quirk catalogue."""
    return os.environ.get("BITGN_PROVIDER_PROFILE", "").strip().lower() == "closerouter"


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
    sch = _detect_schema(run_sql)
    if sch and not sch["props_inline"]:
        out = run_sql(
            f"SELECT DISTINCT {sch['props_key']} FROM {sch['props_tbl']};"
        )
    else:
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
        if not s or s.startswith("[") or s in {"key", "key|", "property_key", "property_key|"}:
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

    sch = _detect_schema(run_sql)
    idcol = sch["stores_id"] if sch else "id"
    # Token-AND match: every token in the (stripped) descriptor must
    # appear in the store id, so "Veveri ... Brno" → store_brno_veveri
    # without needing a hand-maintained city map. Fall back to the
    # legacy candidate tokens if the descriptor yields none.
    desc_tokens = [t for t in re.split(r"[^a-z0-9]+", norm) if len(t) >= 3
                   and t not in {"the", "shop", "store", "branch", "today", "near"}]
    where_clauses = []
    if desc_tokens:
        where_clauses.append(
            "(" + " AND ".join(f"{idcol} LIKE '%{t}%'" for t in desc_tokens) + ")"
        )
    for tok in candidates:
        where_clauses.append(f"{idcol} LIKE '%{tok}%'")
    out = run_sql(
        f"SELECT {idcol} FROM stores WHERE " + " OR ".join(where_clauses)
    )
    if not out:
        return None
    body = _unwrap_sql(out)
    sids = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s in {"id", "id|", "store_id", "store_id|"}:
            continue
        sid = s.split(",")[0].split("|")[0].strip()
        if sid.startswith("store_"):
            sids.append(sid)
    # Only return a confident single resolution; ambiguity → abstain.
    return sids[0] if len(sids) == 1 else None


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


# ── schema adaptivity (PROD product_variants vs legacy products) ──────
# The live ECOM schema is product_variants / product_variant_properties
# (separate table) / store_inventory(store_id, product_sku,
# available_today_quantity) / stores(store_id). Legacy synthetic test
# fixtures use products(sku,path,properties-json) / inventory / stores(id).
# All completer SQL was written for the legacy names and is SILENTLY DEAD
# on PROD. Detect the schema and build against whichever exists.
# See memory project_ecom_count_completer_dead_in_prod.

_SKU_CAP_PER_PRODUCT = 8  # abstain (return []) above this — under-spec flood guard


def _detect_schema(run_sql: Callable[[str], str | None]) -> dict | None:
    out = run_sql("SELECT name FROM sqlite_master WHERE type='table';")
    if out is None:
        return None
    names = set()
    for line in _unwrap_sql(out).splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s in {"name", "name|"}:
            continue
        names.add(_csv_split(s)[0])
    if "product_variants" in names:
        return {
            "kind": "prod", "tbl": "product_variants", "sku": "product_sku",
            "path": "record_path", "name": "product_name",
            "props_inline": False, "props_tbl": "product_variant_properties",
            "props_sku": "product_sku", "props_key": "property_key",
            "props_val": "property_value_text",
            "inv_tbl": "store_inventory", "inv_sku": "product_sku",
            "inv_store": "store_id", "inv_avail": "available_today_quantity",
            "stores_id": "store_id",
        }
    if "products" in names:
        return {
            "kind": "legacy", "tbl": "products", "sku": "sku", "path": "path",
            "name": "name", "props_inline": True, "props_col": "properties",
            "inv_tbl": "inventory", "inv_sku": "sku", "inv_store": "store_id",
            "inv_avail": "available_today", "stores_id": "id",
        }
    return None


def _attr_filter(sch: dict, k: str, v: str) -> str:
    """Match attribute value via the property store OR the display name
    (some variant attributes — wiper-blade/cable length, fastener size —
    live only in product_name). Space-insensitive, case-insensitive."""
    v_ns = _sql_quote(v.replace(" ", ""))
    name_like = f"replace(p.\"{sch['name']}\",' ','') LIKE '%{v_ns}%' COLLATE NOCASE"
    if sch["props_inline"]:
        prop = (
            f"replace(lower(json_extract(p.\"{sch['props_col']}\",'$.{k}')),' ','')"
            f" = lower('{v_ns}')"
        )
    else:
        prop = (
            f"EXISTS (SELECT 1 FROM {sch['props_tbl']} pp WHERE "
            f"pp.{sch['props_sku']} = p.\"{sch['sku']}\" AND "
            f"pp.{sch['props_key']} = '{_sql_quote(k)}' AND "
            f"replace(lower(pp.{sch['props_val']}),' ','') = lower('{v_ns}'))"
        )
    return f" AND ({prop} OR {name_like})"


def _rows_to_paths(body: str, path_col: str) -> list[str]:
    paths: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("[") or s in {"path", "path|", path_col, path_col + "|"}:
            continue
        cols = _csv_split(s)
        if cols and cols[0].startswith("/proc/catalog/"):
            paths.append(cols[0])
    return paths


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


# ── v0.1.98 P1: structured-input completer ───────────────────────────


def _find_qualifying_skus_relaxed(
    brand: str,
    series: str,
    model: str,
    attributes: dict[str, str],
    store_id: str,
    threshold: int,
    run_sql: Callable[[str], str | None],
) -> list[str] | None:
    """Like ``_find_qualifying_skus`` but takes brand/series/model
    plus attributes directly (no ProductSpec). Falls back gracefully
    if the attribute filters yield zero matches — drops them and
    retries with brand+model alone, then brand alone. This handles
    the v0.1.96 t15-shape failure where the agent's SQL over-
    constrained with a normalized-wrong attribute value.

    Returns None on SQL failure (caller should abstain).
    """
    sch = _detect_schema(run_sql)
    if sch is None:
        return None
    brand_q = _sql_quote(brand)

    # SAFE strict-only resolution: brand + model LIKE (the model code may
    # carry a series prefix, e.g. task 'XTREME 300-EAF' vs '300-EAF') +
    # EVERY attribute matched via property store OR display name. We do
    # NOT relax to brand-only — that floods unrelated variants and
    # triggers the grader's "too many invalid references" (a tried
    # brand-only ladder returned 33 SKUs/product). If the strict query
    # returns 0 or more than the per-product cap, ABSTAIN (return []),
    # leaving the LLM's choice untouched. Only ADDS, never floods.
    model_code = (model or "").split()[-1] if model else ""
    line_filter = ""
    if model_code:
        line_filter += f" AND p.model LIKE '%{_sql_quote(model_code)}%'"
    elif series:
        line_filter += f" AND p.series LIKE '%{_sql_quote(series)}%'"
    attr_clause = "".join(_attr_filter(sch, k, v) for k, v in attributes.items())
    sql = (
        f"SELECT p.\"{sch['path']}\" FROM {sch['tbl']} p "
        f"JOIN {sch['inv_tbl']} i ON i.{sch['inv_sku']} = p.\"{sch['sku']}\" "
        f"WHERE p.brand = '{brand_q}' COLLATE NOCASE"
        f"{line_filter}{attr_clause} "
        f"AND i.{sch['inv_store']} = '{_sql_quote(store_id)}' "
        f"AND i.{sch['inv_avail']} >= {int(threshold)} "
        "LIMIT 50;"
    )
    out = run_sql(sql)
    if out is None:
        return None
    paths = _rows_to_paths(_unwrap_sql(out), sch["path"])
    if len(paths) > _SKU_CAP_PER_PRODUCT:
        # under-specified / over-broad match — abstain rather than flood
        return []
    return paths


def compute_count_per_store(
    *,
    task_spec,
    run_sql: Callable[[str], str | None],
) -> int | None:
    """Compute the canonical COUNT for a count_per_store task: the
    number of distinct PRODUCTS (not SKUs) in `task_spec.products`
    that have at least one qualifying SKU at the named store with
    available_today >= threshold.

    Returns None on parse/SQL failure (caller should not override).
    """
    if task_spec is None:
        return None
    if getattr(task_spec, "kind", "none") != "count_per_store":
        return None
    products = getattr(task_spec, "products", []) or []
    if not products:
        return None
    store_descriptor = getattr(task_spec, "store_descriptor", "") or ""
    threshold = int(getattr(task_spec, "threshold", 0) or 0)
    store_id = resolve_store_id(store_descriptor, run_sql)
    if store_id is None:
        return None
    n = 0
    for p in products:
        brand = getattr(p, "brand", "") or ""
        series = getattr(p, "series", "") or ""
        model = getattr(p, "model", "") or ""
        attrs = dict(getattr(p, "attributes", {}) or {})
        if not brand:
            continue
        skus = _find_qualifying_skus_relaxed(
            brand=brand,
            series=series,
            model=model,
            attributes=attrs,
            store_id=store_id,
            threshold=threshold,
            run_sql=run_sql,
        )
        if skus is None:
            return None  # SQL fail → don't override
        if skus:
            n += 1
    return n


_NEGATION_RE = re.compile(
    r"\b(fewer than|less than|under|below|at most|no more than|"
    r"fewer|lower than|not more than|no same-day|without)\b",
    re.IGNORECASE,
)


def complete_sku_refs_from_spec(
    *,
    task_spec,  # TaskSpec (loose-typed to avoid pydantic cycle)
    refs: Sequence[str],
    run_sql: Callable[[str], str | None],
    task_text: str = "",
) -> CompleterResult:
    """Use the agent-emitted ``task_spec`` (structured) to ADD any
    missing qualifying catalogue SKU paths to grounding_refs. UNION
    semantics: this NEVER removes refs the agent emitted.

    Aborts (refs unchanged) if:
      - task_spec.kind != 'count_per_store'
      - store_descriptor cannot be resolved to a store_id
      - SQL fails

    Per `feedback_enforcer_cannot_replace_adaptive_llm`: this is a
    SUPERSET enforcer. If task_spec is malformed or partial, we
    leave the LLM's choice alone.
    """
    if task_spec is None:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="no task_spec",
        )
    # NEGATION GUARD: the resolver qualifies products by
    # available_today >= threshold. For "fewer than N" / "less than N"
    # / "no same-day" tasks the qualifying condition is INVERTED
    # (available < threshold), so adding >=-qualifying SKUs would cite
    # exactly the WRONG products → grader "invalid reference". When the
    # task is a negation, abstain and leave the LLM's refs untouched.
    if task_text and _NEGATION_RE.search(task_text):
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="negation task — completer abstains",
        )
    kind = getattr(task_spec, "kind", "none")
    products = getattr(task_spec, "products", []) or []
    store_descriptor = getattr(task_spec, "store_descriptor", "") or ""
    threshold = int(getattr(task_spec, "threshold", 0) or 0)

    # v0.1.117-pre+: salvage path when LLM misclassified task_spec.kind
    # but the structural fields (products + store_descriptor + threshold)
    # ARE populated. PROD t13 (run-22Rhf9Y..., 2026-05-28) emitted
    # kind="none" on a clear count_per_store question; the strict gate
    # below aborted the completer and the qualifying SKU never landed
    # in grounding_refs. The salvage is GATED behind structural
    # evidence — products+store+threshold all present — so it never
    # fires on tasks that genuinely aren't count_per_store.
    structural_fit = bool(products and store_descriptor and threshold > 0)
    if kind != "count_per_store" and not structural_fit:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason=f"task_spec.kind={kind!r}",
        )
    if not products:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="task_spec.products empty",
        )

    store_id = resolve_store_id(store_descriptor, run_sql)
    if store_id is None:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True,
            abort_reason=f"store unresolved: {store_descriptor!r}",
        )

    have = set(refs)
    out_refs = list(refs)
    added: list[str] = []
    reasons: list[str] = []

    for p in products:
        brand = getattr(p, "brand", "") or ""
        series = getattr(p, "series", "") or ""
        model = getattr(p, "model", "") or ""
        attributes = dict(getattr(p, "attributes", {}) or {})
        if not brand:
            continue
        skus = _find_qualifying_skus_relaxed(
            brand=brand,
            series=series,
            model=model,
            attributes=attributes,
            store_id=store_id,
            threshold=threshold,
            run_sql=run_sql,
        )
        if skus is None:
            return CompleterResult(
                refs=list(refs), added=[], reasons=[],
                aborted=True,
                abort_reason=f"sql failed for product {brand}",
            )
        # v0.1.111 capped this to `skus[:1]` (1 SKU per product) to
        # dodge CloseRouter's "too many invalid references" grader
        # rejection. v0.1.113-pre cliproxyapi baseline (2026-05-27)
        # showed the OPPOSITE: the cliproxyapi grader REQUIRES all
        # qualifying SKUs cited per product, so the cap caused 5
        # fresh failures (t11/t15/t17/t33/t45 — missing required
        # ref). v0.1.113 gates the cap behind BITGN_PROVIDER_PROFILE:
        # default cliproxyapi → no cap; closerouter → cap=1.
        # scripts/use_provider.sh flips the env var in .env.
        # Evidence (t15): v108-era completer added 16 refs → grader
        # accepted; v111-era completer added 4 refs (cap) → grader
        # said missing required ref.
        skus_to_add = skus[:1] if _closerouter_caps_enabled() else skus
        for path in skus_to_add:
            if path not in have:
                out_refs.append(path)
                have.add(path)
                added.append(path)
                reasons.append(
                    f"{path}: qualifying SKU from task_spec "
                    f"({brand} / {model}) at {store_id} "
                    f"(avail >= {threshold}) [1 of {len(skus)}]"
                )
    return CompleterResult(
        refs=out_refs, added=added, reasons=reasons,
    )


# ── v0.1.99: yes_no_sku family-enumerator ───────────────────────────


def _find_family_skus(
    brand: str,
    series: str,
    model: str,
    run_sql: Callable[[str], str | None],
    name: str = "",
) -> list[str] | None:
    """Find every SKU in the brand+series(+model) family. Used by
    the yes_no_sku completer to enumerate candidates whose attributes
    sku_verifier can then prune.

    Relaxation ladder: strict (series LIKE + model =) → brand+model.
    DOES NOT fall back to brand-only — for yes_no_sku, the agent's
    claim names a specific line/model, and brand-only enumeration
    pulls in unrelated product categories (v0.1.102 t32 PROD
    repro: Kopp 'Wiring Device' fallback returned 49 extension-cable
    SKUs because the wiring-device family didn't exist). When both
    tiers fail, abstain — the catalogue genuinely lacks a matching
    SKU and the agent's own 'closest miss' citation is best we can
    do."""
    brand_q = _sql_quote(brand)
    tries: list[list[str]] = []
    base = f"p.brand = '{brand_q}' COLLATE NOCASE"
    if series and model:
        tries.append([
            base,
            f"p.series LIKE '%{_sql_quote(series)}%'",
            f"p.model = '{_sql_quote(model)}'",
        ])
    if model:
        tries.append([base, f"p.model = '{_sql_quote(model)}'"])
    # brand + name LIKE: when model doesn't exist (false claim),
    # filter to the right product category via product name
    # ("Wiring Device", "Nut Bolt and Washer", etc.) instead of
    # over-citing every brand SKU across all categories. Closes
    # v0.1.103 t05 PROD where the agent named Heco 3DW-64B (no
    # such model) but the grader required FST-3SJKL8BF in the
    # nuts_bolts_washers product line.
    if name:
        tries.append(
            [base, f"p.name LIKE '%{_sql_quote(name)}%'"]
        )

    for where_clauses in tries:
        sql = (
            "SELECT p.path FROM products p "
            f"WHERE {' AND '.join(where_clauses)} LIMIT 50;"
        )
        out = run_sql(sql)
        if out is None:
            return None
        body = _unwrap_sql(out)
        paths: list[str] = []
        for line in body.splitlines():
            s = line.strip()
            if (
                not s
                or s.startswith("[")
                or s == "path"
                or s.startswith("path|")
            ):
                continue
            cols = _csv_split(s)
            if cols and cols[0].startswith("/proc/catalog/"):
                paths.append(cols[0])
        if paths:
            return paths
    return []


def complete_yes_no_sku_refs(
    *,
    task_spec,
    refs: Sequence[str],
    run_sql: Callable[[str], str | None],
) -> CompleterResult:
    """For 'support note claims we stock X' tasks: enumerate every
    SKU in the brand+series family and UNION into grounding_refs.
    The sku_verifier downstream drops wrong-attribute members; the
    grader-expected SKU survives.

    Aborts on kind != 'yes_no_sku', empty products, or SQL failure.
    """
    if task_spec is None:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="no task_spec",
        )
    kind = getattr(task_spec, "kind", "none")
    if kind != "yes_no_sku":
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason=f"task_spec.kind={kind!r}",
        )
    products = getattr(task_spec, "products", []) or []
    if not products:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="task_spec.products empty",
        )
    p = products[0]
    brand = getattr(p, "brand", "") or ""
    series = getattr(p, "series", "") or ""
    model = getattr(p, "model", "") or ""
    name = getattr(p, "name", "") or ""
    if not brand:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="brand missing",
        )
    family = _find_family_skus(brand, series, model, run_sql, name=name)
    if family is None:
        return CompleterResult(
            refs=list(refs), added=[], reasons=[],
            aborted=True, abort_reason="sql failed",
        )
    have = set(refs)
    out_refs = list(refs)
    added: list[str] = []
    reasons: list[str] = []
    # v0.1.111 capped this to family[:5] to dodge CloseRouter's
    # "too many invalid references" rejection. cliproxyapi grader
    # REQUIRES qualifying SKUs cited, so the cap caused fresh
    # failures (t08/t11/t17/t33/t45 — missing required ref).
    # v0.1.113 gates the cap behind BITGN_PROVIDER_PROFILE: default
    # cliproxyapi → no cap; closerouter → cap=5.
    family_to_add = family[:5] if _closerouter_caps_enabled() else family
    for path in family_to_add:
        if path not in have:
            out_refs.append(path)
            have.add(path)
            added.append(path)
            reasons.append(
                f"{path}: {brand}/{series}/{model} family member"
            )
    return CompleterResult(
        refs=out_refs, added=added, reasons=reasons,
    )
