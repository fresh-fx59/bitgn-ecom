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
    for prop_name, prop_value in props.items():
        if not isinstance(prop_value, str) or not prop_value:
            continue
        if not isinstance(prop_name, str):
            continue
        prop_label = prop_name.replace("_", " ").lower()
        if prop_label not in task_text_norm:
            continue  # task does not specify this attribute, skip
        value_norm = _normalize(prop_value)
        if value_norm in task_text_norm:
            continue  # match
        return (
            f"property {prop_name}={prop_value!r} "
            f"not in task spec"
        )
    return None


ReadSku = Callable[[str], str | None]


def filter_sku_refs(
    *,
    task_text: str,
    refs: Sequence[str],
    read_sku: ReadSku,
) -> FilterResult:
    """Drop cited ``/proc/catalog/*.json`` refs whose attributes
    contradict the task spec.

    ``read_sku`` is a callable taking a path and returning the file
    contents as a string (or None if the read failed). The caller
    wires it to the active EcomAdapter.
    """
    if not task_text or not refs:
        return FilterResult(kept=list(refs), dropped=[], reasons=[])

    task_norm = _normalize(task_text)
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
        mismatch = sku_mismatches_task(sku_json, task_norm)
        if mismatch is None:
            kept.append(ref)
        else:
            dropped.append(ref)
            reasons.append(f"{ref}: {mismatch}")
    return FilterResult(kept=kept, dropped=dropped, reasons=reasons)
