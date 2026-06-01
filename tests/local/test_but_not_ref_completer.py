"""Tests for the "(but not <SKU>)" exclusion-citation completer.

Grader evidence (PROD v0.1.167 rerun score_detail):
  * t002 "...(but not PT-WASH-KAR-K4-PIPE)..." -> missing
    [/proc/catalog/Karcher/PT-WASH-KAR-K4-PIPE.json]
  * t062 "...(but not PT-MOW-STI-RMA235-AK30)..." -> missing
    [/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK30.json]

For YES/NO availability questions that NAME a SKU after "but not", the grader
requires that SKU's /proc/catalog record CITED (proof you looked it up to
exclude it). The agent excludes it from consideration and never cites it, so it
under-cites. This completer ADDS the named excluded SKU's record (union — never
removes, never rewrites the answer). The SKU is resolved against the live
catalogue so a non-existent token is dropped.
"""
from __future__ import annotations

from bitgn_contest_agent.but_not_ref_completer import (
    applies, excluded_skus, complete_refs,
)

# Real PROD phrasings (v0.1.167).
_T062 = ("Do you have 4 of 'Stihl RMA 235 kit. AK battery level was not "
         "provided.' (but not PT-MOW-STI-RMA235-AK30) in stock in PowerTools "
         "at Eggenberg?")
_T002 = ("Do you have 8 of 'karcher k4 specialist accessory set without home "
         "car kit' (but not PT-WASH-KAR-K4-PIPE) in stock in PowerTools near "
         "Graz Center?")

# A faithful resolve_fn backed by the real scraped catalogue filenames.
_CATALOG = {
    "PT-MOW-STI-RMA235-AK30": "/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK30.json",
    "PT-MOW-STI-RMA235-AK20": "/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK20.json",
    "PT-WASH-KAR-K4-PIPE": "/proc/catalog/Karcher/PT-WASH-KAR-K4-PIPE.json",
    "PT-WASH-KAR-K4-PC": "/proc/catalog/Karcher/PT-WASH-KAR-K4-PC.json",
}
def _resolve(sku):
    return _CATALOG.get(sku)


def test_extracts_excluded_sku():
    assert excluded_skus(_T062) == ["PT-MOW-STI-RMA235-AK30"]
    assert excluded_skus(_T002) == ["PT-WASH-KAR-K4-PIPE"]


def test_applies_to_availability_with_exclusion():
    assert applies(_T062) is True
    assert applies(_T002) is True


def test_adds_missing_excluded_record_t062():
    # agent cited the resolved product (AK20) but not the excluded AK30
    existing = ["/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK20.json"]
    added = complete_refs(_T062, existing, _resolve)
    assert added == ["/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK30.json"]


def test_adds_missing_excluded_record_t002():
    existing = ["/proc/catalog/Karcher/PT-WASH-KAR-K4-PC.json"]
    added = complete_refs(_T002, existing, _resolve)
    assert added == ["/proc/catalog/Karcher/PT-WASH-KAR-K4-PIPE.json"]


def test_no_duplicate_when_already_cited():
    existing = ["/proc/catalog/Stihl/PT-MOW-STI-RMA235-AK30.json"]
    assert complete_refs(_T062, existing, _resolve) == []


def test_abstains_when_sku_unresolvable():
    assert complete_refs(_T062, [], lambda s: None) == []


def test_does_not_apply_without_but_not():
    txt = "Do you have 4 of Stihl RMA 235 kit in stock at Eggenberg?"
    assert applies(txt) is False
    assert complete_refs(txt, [], _resolve) == []


def test_does_not_apply_to_count_list_family():
    # "how many of these: A, B" is owned by count_ref_completer; no "but not".
    txt = ("how many of these SKUs ...: PT-A-1, PT-B-2, PT-C-3? Answer \"%d\".")
    assert applies(txt) is False


def test_does_not_apply_to_non_availability_but_not():
    # a prose "but not" with no SKU token must not fire
    txt = "Refund the order but not the shipping fee."
    assert applies(txt) is False
    assert excluded_skus(txt) == []
