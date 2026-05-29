"""SKU attribute verifier — post-pass enforcer that drops cited
``/proc/catalog/*.json`` paths whose ``properties`` contradict the
task's product spec.

Fixes the v0.1.61 – v0.1.64 SKU-pick variance on count tasks
(t13/t14/t15/t16 family). The LLM identifies the qualifying SKU
correctly MOST of the time, but on count tasks that list several
multi-spec products it occasionally cites a same-brand/same-line
wrong-attribute variant. The grader then flags
``answer contains invalid reference '<path>'``. This module
deterministically validates each cited SKU against the task's
attribute spec and drops the mismatches before submission.

Conservative by design:

* Only inspects refs under ``/proc/catalog/``.
* A SKU is dropped only when its ``brand`` AND ``series`` (or
  ``model``) appear verbatim in the task text AND at least one of
  its ``properties`` values contradicts a task-mentioned attribute.
* If the SKU's brand/series do not appear in the task text, the
  SKU is left alone — it may have been cited for another reason
  (e.g. the task is about a different brand entirely and the agent
  legitimately cited this one as context).
* Failures to read or parse a SKU JSON leave the ref untouched.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class FilterResult:
    kept: list[str]
    dropped: list[str]
    reasons: list[str]


_WS = re.compile(r"\s+")


def _normalize(s: str) -> str:
    return _WS.sub(" ", s.lower()).strip()


def sku_mismatches_task(
    sku_json: dict, task_text_norm: str
) -> str | None:
    """Return a mismatch reason if SKU contradicts task spec, else None.

    ``task_text_norm`` must already be lower-cased and whitespace-
    normalised (use :func:`_normalize`).
    """
    if not isinstance(sku_json, dict):
        return None

    brand = (sku_json.get("brand") or "").strip()
    series = (sku_json.get("series") or "").strip()
    model = (sku_json.get("model") or "").strip()
    props = sku_json.get("properties")

    # Gate: brand+(series or model) must appear in the task. If not,
    # the SKU is unrelated to any task product and we abstain.
    if not brand or brand.lower() not in task_text_norm:
        return None
    line_in_task = (
        series and series.lower() in task_text_norm
    ) or (model and model.lower() in task_text_norm)
    if not line_in_task:
        return None

    # Now check property values.
    if not isinstance(props, dict):
        return None

    # Build a label-search text that EXCLUDES the product's own
    # brand/series/model tokens. A property NAME that only appears as
    # part of the line name (e.g. property `stackable` vs the series
    # "Festool Stackable", `flexible` vs "Bostik Flexible Fix") is NOT
    # a task-specified attribute — it's the product's name. Matching
    # such a label and then demanding its value be present in the task
    # text falsely strips the CORRECT SKU (v-2026-05 t01: stripped
    # STO-2R84BSHQ for stackable='yes' although the task only required
    # storage_type='parts case'). Value matching still uses the FULL
    # task text so genuine attribute values (which may also appear in
    # the line name, e.g. "parts case") still match.
    label_search_text = task_text_norm
    for token in (brand, series, model):
        t = _normalize(token)
        if t:
            label_search_text = label_search_text.replace(t, " ")

    for prop_name, prop_value in props.items():
        if not isinstance(prop_value, str) or not prop_value:
            continue
        if not isinstance(prop_name, str):
            continue
        prop_label = prop_name.replace("_", " ").lower()
        if prop_label not in label_search_text:
            continue  # task does not specify this attribute, skip
        value_norm = _normalize(prop_value)
        if value_norm in task_text_norm:
            continue  # match
        return (
            f"property {prop_name}={prop_value!r} "
            f"not in task spec"
        )
    return None


def _spec_token(s: object) -> str:
    return _normalize(s if isinstance(s, str) else "")


def _match_spec_product(sku_json: dict, products: Sequence) -> object | None:
    """Find the task_spec product this SKU belongs to.

    Match on brand AND (model or series). Returns the matched product
    object, or None if the SKU does not clearly belong to any listed
    product (then the caller abstains — never strips).
    """
    sku_brand = _spec_token(sku_json.get("brand"))
    sku_series = _spec_token(sku_json.get("series"))
    sku_model = _spec_token(sku_json.get("model"))
    if not sku_brand:
        return None
    for p in products:
        p_brand = _spec_token(getattr(p, "brand", ""))
        if not p_brand or p_brand != sku_brand:
            continue
        p_model = _spec_token(getattr(p, "model", ""))
        p_series = _spec_token(getattr(p, "series", ""))
        # model is the strongest disambiguator; the task model may
        # carry the series prefix ("Valena 2T3-OA7") so allow either
        # direction of containment. Fall back to series overlap.
        model_ok = bool(sku_model) and bool(p_model) and (
            sku_model in p_model or p_model in sku_model
        )
        series_ok = bool(sku_series) and bool(p_series) and (
            sku_series in p_series or p_series in sku_series
        )
        if model_ok or series_ok:
            return p
    return None


def sku_mismatches_spec(sku_json: dict, product) -> str | None:
    """Return a mismatch reason if the SKU contradicts a SPECIFIC
    product's structured attributes, else None.

    Only the attributes the task explicitly named for THIS product are
    checked (``product.attributes``), so neither the product's own
    extra properties (t01: stackable) nor another product's attribute
    label in a multi-product task (t16: a different row's ip_rating)
    can falsely strip the correct SKU. A mismatch is reported only
    when a named attribute is present on the SKU with a DIFFERENT
    value — the same conservative direction as the text heuristic.
    """
    if not isinstance(sku_json, dict):
        return None
    props = sku_json.get("properties")
    if not isinstance(props, dict):
        return None
    attrs = getattr(product, "attributes", None) or {}
    for key, req_val in attrs.items():
        if not isinstance(key, str) or not isinstance(req_val, str) or not req_val:
            continue
        sku_val = props.get(key)
        if not isinstance(sku_val, str) or not sku_val:
            continue  # SKU does not expose this attr → abstain (keep)
        req_norm = _normalize(req_val).replace(" ", "")
        sku_norm = _normalize(sku_val).replace(" ", "")
        if req_norm and sku_norm and req_norm != sku_norm:
            return f"property {key}={sku_val!r} != task spec {req_val!r}"
    return None


ReadSku = Callable[[str], str | None]


def filter_sku_refs(
    *,
    task_text: str,
    refs: Sequence[str],
    read_sku: ReadSku,
    spec_products: Sequence | None = None,
) -> FilterResult:
    """Drop cited ``/proc/catalog/*.json`` refs whose attributes
    contradict the task spec.

    ``read_sku`` is a callable taking a path and returning the file
    contents as a string (or None if the read failed). The caller
    wires it to the active EcomAdapter.

    When ``spec_products`` (the agent's structured ``task_spec.products``)
    is supplied, each SKU is matched to its specific product and checked
    against ONLY that product's named attributes. This is strictly more
    precise than substring-matching the global task text and avoids the
    cross-product / series-name false positives. The text heuristic is
    the fallback when no matching product is found.
    """
    if not refs:
        return FilterResult(kept=list(refs), dropped=[], reasons=[])

    task_norm = _normalize(task_text) if task_text else ""
    products = list(spec_products) if spec_products else []
    kept: list[str] = []
    dropped: list[str] = []
    reasons: list[str] = []

    for ref in refs:
        if not ref.startswith("/proc/catalog/"):
            kept.append(ref)
            continue
        try:
            content = read_sku(ref)
        except Exception:
            kept.append(ref)
            continue
        if not content:
            kept.append(ref)
            continue
        try:
            sku_json = json.loads(content)
        except Exception:
            kept.append(ref)
            continue

        mismatch: str | None = None
        matched_product = _match_spec_product(sku_json, products) if products else None
        if matched_product is not None:
            mismatch = sku_mismatches_spec(sku_json, matched_product)
        elif task_norm:
            mismatch = sku_mismatches_task(sku_json, task_norm)

        if mismatch is None:
            kept.append(ref)
        else:
            dropped.append(ref)
            reasons.append(f"{ref}: {mismatch}")
    return FilterResult(kept=kept, dropped=dropped, reasons=reasons)
