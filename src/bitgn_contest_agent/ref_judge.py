"""LLM-as-judge grounding_ref corrector for the PROD catalogue-citing families.

The PROD grader checks `answer refs for family "/proc/catalog"` as an EXACT set
(a single missing OR extra ref → score 0; see memory
project_ecom_ref_completeness_dominant). Which catalogue records belong in the
set is SEMANTIC and depends on the task's intent:

  * COUNT over a SKU list ("how many of these SKUs ...: <list>") → cite the
    /proc/catalog record of EVERY candidate SKU listed (qualifying or not).
  * YES/NO EXISTENCE ("does <brand> <line> with <specs> exist") → cite ONLY the
    record(s) matching ALL specs; NOT near-miss candidates examined-then-rejected.
  * YES/NO AVAILABILITY ("do you have N of X (but not Y)") → cite the resolved
    product X; never the excluded Y.

A brittle regex can't make this call, so this is an LLM-as-judge (per
best-practice: analytic rubric, reference-anchored on the candidate records,
structured JSON output, chain-of-thought). It returns the corrected
/proc/catalog ref set; the caller swaps the answer's catalogue refs for it
(non-catalogue refs untouched). Gated default-off (BITGN_USE_REF_JUDGE). The
judge is reference-anchored: it decides ONLY from the candidate records handed
to it, so it cannot hallucinate paths.
"""
from __future__ import annotations

import os
from typing import Optional

from bitgn_contest_agent import classifier

_SYSTEM = """\
You are a grounding-reference auditor for an e-commerce agent. The grader \
requires the answer's grounding_refs to cite EXACTLY the correct set of \
/proc/catalog product records for the task — no missing, no extra. A single \
wrong ref scores the whole answer 0. Decide the correct set from the candidate \
records provided (cite paths ONLY from that list — never invent a path).

Apply the ONE rule matching the task:
- DOES-NOT-EXIST / NO answer (the answer is FALSE / FALSE(n) / <NO> / "nein" / \
"no" — the product does NOT exist): the correct set is EMPTY. Cite NO catalog \
record — do NOT cite a near-miss you examined and rejected. (Verified: grader \
marks such a near-miss as an EXTRA ref → score 0.)
- YES/NO EXISTENCE answered YES ("does <brand> <line> with <specs> exist?"): \
cite ONLY the record(s) matching ALL stated specs; not near-misses that fail a \
spec.
- YES/NO AVAILABILITY ("do you have N of <product> ..."): cite the record of \
the product the request RESOLVES TO (the variant matching the description). A \
"(but not <SKU>)" clause constrains the quantity/availability check, NOT the \
citation — do NOT drop a SKU just because it appears after "but not".
If you cannot confidently resolve which records belong, keep exactly the \
catalog refs the agent already cited.

Think step by step, then output JSON only:
{"reasoning": "<one or two sentences>", "catalog_refs": ["/proc/catalog/...", ...]}"""


import json as _json
import re as _re

# YES/NO families only (existence + availability). Count-list ("how many of
# these SKUs") is owned DETERMINISTICALLY by count_ref_completer, which runs
# BEFORE this judge and cites every candidate; the judge must NOT fire there
# (its yes/no "cite only the match" rubric could prune the completer's correct
# additions → regression). So this signal deliberately EXCLUDES "how many".
_FAMILY_SIGNAL = _re.compile(
    r"does such product exist|does (the|this|that|a)\b|do you have|"
    r"is there a|do we (have|stock)|can i buy",
    _re.I)
_SKU_TOKEN = _re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\b")


def is_enabled() -> bool:
    return os.environ.get("BITGN_USE_REF_JUDGE", "").strip() == "1"


def _is_catalog(path: str) -> bool:
    return isinstance(path, str) and path.startswith("/proc/catalog/")


def applies(task_text: str, current_refs: list[str]) -> bool:
    """Fire only on catalogue-citing families (count / yes-no existence /
    availability) where the /proc/catalog ref SET depends on intent — and
    only when there is something to reason about (the agent cited a catalogue
    ref, or the task names a SKU). Avoids touching unrelated tasks."""
    if not task_text or not _FAMILY_SIGNAL.search(task_text):
        return False
    has_catalog_ref = any(_is_catalog(p) for p in (current_refs or []))
    return has_catalog_ref or bool(_SKU_TOKEN.search(task_text))


def gather_candidates(task_text, current_refs, resolve_fn, read_fn) -> list[dict]:
    """Collect candidate catalogue records (path+sku+brand+name+attributes) the
    judge reasons over: the agent's own /proc/catalog refs PLUS every SKU named
    in the task, resolved via ``resolve_fn(sku)`` (an EXACT filename `find`, not
    content search — see count_ref_completer for why). Reads each record for
    attributes so the judge can decide spec matches (yes/no)."""
    paths: list[str] = [p for p in (current_refs or []) if _is_catalog(p)]
    for m in _SKU_TOKEN.finditer(task_text or ""):
        sku = m.group(1)
        try:
            p = resolve_fn(sku)
        except Exception:
            p = None
        if p and p not in paths:
            paths.append(p)
    out: list[dict] = []
    seen = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        rec = {"path": p, "sku": p.rsplit("/", 1)[-1][:-5],
               "brand": p.split("/proc/catalog/", 1)[-1].split("/", 1)[0], "name": "", "attributes": {}}
        try:
            content = read_fn(p)
            if content:
                obj = _json.loads(content)
                rec["name"] = obj.get("name", "")
                rec["sku"] = obj.get("sku", rec["sku"])
                rec["brand"] = obj.get("brand", rec["brand"])
                rec["attributes"] = obj.get("properties") or {}
        except Exception:
            pass
        out.append(rec)
    return out


def judge_catalog_refs(
    task_text: str,
    agent_answer: str,
    current_catalog_refs: list[str],
    candidate_records: list[dict],
    *,
    classify_fn=None,
) -> Optional[list[str]]:
    """Return the corrected list of /proc/catalog refs, or None to ABSTAIN
    (leave the agent's refs unchanged). `candidate_records` is a list of
    {path, sku, brand, name, attributes} dicts (the evidence). `classify_fn`
    is injectable for tests (defaults to classifier.classify)."""
    if not candidate_records:
        return None
    classify_fn = classify_fn or classifier.classify
    lines = []
    for r in candidate_records:
        attrs = r.get("attributes") or {}
        attr_s = ", ".join(f"{k}={v}" for k, v in attrs.items())
        lines.append(
            f"- path={r.get('path')} sku={r.get('sku')} brand={r.get('brand')} "
            f"name={r.get('name')!r}" + (f" attrs[{attr_s}]" if attr_s else ""))
    user = (
        f"TASK:\n{task_text}\n\n"
        f"AGENT ANSWER: {agent_answer!r}\n\n"
        f"AGENT'S CURRENT /proc/catalog refs:\n"
        + ("\n".join(f"- {p}" for p in current_catalog_refs) or "(none)")
        + "\n\nCANDIDATE CATALOG RECORDS (cite paths only from here):\n"
        + "\n".join(lines)
    )
    try:
        out = classify_fn(system=_SYSTEM, user=user)
    except Exception:
        return None
    if not isinstance(out, dict):
        return None
    refs = out.get("catalog_refs")
    if not isinstance(refs, list):
        return None
    # safety: keep only valid catalog paths that exist in the candidate set
    # (reference-anchored — the judge cannot introduce a path we didn't supply)
    allowed = {r.get("path") for r in candidate_records} | set(current_catalog_refs)
    corrected = [p for p in refs if _is_catalog(p) and p in allowed]
    if not corrected:
        # Empty set is a VALID correction ONLY for an EXISTENCE question
        # ("does X exist?") answered NO — there cite nothing (drop the
        # near-miss the agent wrongly cited; t006/t046). For AVAILABILITY
        # ("do you have N of X?") or OCR ("can I buy this basket?") negatives
        # the product DOES exist and the grader wants its record cited, so an
        # empty LLM result there is treated as ABSTAIN (keep the agent's refs)
        # — never strip. (Evidence: PROD v164 ref_judge wrongly stripped t002
        # availability + t063 OCR to [].) On any positive answer empty = ABSTAIN.
        if _is_negative_answer(agent_answer) and _is_existence_question(task_text):
            return []
        return None
    return corrected


_NEG_ANSWER = _re.compile(r"\bfalse\b|false\s*\(|<\s*no\s*>|\bnein\b|\bno\b|\bnein\b", _re.I)

# EXISTENCE question = asks whether a product matching the specs EXISTS at all.
# AVAILABILITY ("do you have N", "how many", "in stock", "units") and OCR
# ("buy this basket", "receipt") ask about stock of a product that DOES exist
# — there a NO answer must still cite the resolved product, so they are NOT
# existence questions and must not trigger the cite-nothing strip.
_AVAILABILITY = _re.compile(
    r"do you have\b|how many\b|in stock\b|\bunits?\b|available\b|"
    r"buy this\b|receipt\b|same basket\b", _re.I)
_EXISTENCE = _re.compile(
    r"does such (a )?product exist|does (the|this|that|a|such)\b[^?]*\bexist|"
    r"\bexist\b|is there a\b|do we (stock|carry|offer)\b|"
    r"does .* (line|set|kit|model)\b", _re.I)


def _is_existence_question(task_text: str) -> bool:
    t = task_text or ""
    if _AVAILABILITY.search(t):
        return False
    return bool(_EXISTENCE.search(t))


def _is_negative_answer(answer: str) -> bool:
    """True when the answer asserts the product does NOT exist / is unavailable
    (FALSE / FALSE(n) / <NO> / nein / no)."""
    a = (answer or "").strip().lower()
    if not a:
        return False
    return bool(_NEG_ANSWER.search(a))


def apply_correction(all_refs: list[str], corrected_catalog: list[str]) -> list[str]:
    """Swap the /proc/catalog refs in `all_refs` for `corrected_catalog`,
    preserving non-catalogue refs and original order where possible."""
    non_catalog = [p for p in all_refs if not _is_catalog(p)]
    seen, out = set(), []
    for p in non_catalog + list(corrected_catalog):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
