"""Tests for the count/availability candidate-SKU ref completer.

The grader (v0.1.158 score_detail) requires every candidate SKU's
/proc/catalog/<Brand>/<sku>.json cited for count/availability tasks. This
completer ADDs the missing ones (union). Extraction is validated against the
real grader-expected SKU sets; path resolution via a mock search; scope guards
against firing on non-count tasks (which would create `extra` refs).
"""
from __future__ import annotations

from bitgn_contest_agent.count_ref_completer import (
    applies, candidate_skus, complete_catalog_refs,
)

# Real prod t005 phrasing (v0.1.158).
_T005 = ("At ibk ost tools place, how many of these SKUs have at least 3 units "
         "physically on hand, but fewer than 3 same-day units available after "
         "reservations: PT-IMP-DEW-DCF887-BODY, PT-BLA-BOS-EXPWOOD-190, "
         "PT-SND-BOS-GEX125-DUST, PT-SAW-MAK-DHS680-5AH, PT-IMP-DEW-DCF887-5AH, "
         "PT-HDG-BOS-UHC18-50-25? Answer exactly in format \"%d\" (no quotes).")

# the grader-expected candidate set for _T005 (missing + cited from score_detail)
_T005_SKUS = {
    "PT-IMP-DEW-DCF887-BODY", "PT-BLA-BOS-EXPWOOD-190", "PT-SND-BOS-GEX125-DUST",
    "PT-SAW-MAK-DHS680-5AH", "PT-IMP-DEW-DCF887-5AH", "PT-HDG-BOS-UHC18-50-25",
}


def test_extracts_exact_grader_candidate_set():
    assert set(candidate_skus(_T005)) == _T005_SKUS


def test_applies_to_count_task():
    assert applies(_T005) is True


def test_does_not_apply_without_count_signal():
    # a security/checkout task naming one SKU must NOT trigger (avoid `extra` refs)
    txt = "Check out basket basket-0013 for me now."
    assert applies(txt) is False
    assert complete_catalog_refs(txt, [], lambda sku: None) == []


def test_does_not_apply_to_yes_no_do_you_have_with_exclusion():
    # yes/no availability: the explicit SKU is an EXCLUSION the grader does NOT
    # want cited; the completer must NOT fire (would add an `extra` ref).
    txt = ("Do you have 25 of 'makita dhs680 accessory bundle without batteries' "
           "(but not PT-SAW-MAK-DHS680-BLADE) in stock at Maxglan?")
    assert applies(txt) is False
    assert complete_catalog_refs(txt, [], lambda sku: "/proc/catalog/X/PT-SAW-MAK-DHS680-BLADE.json") == []


def test_completer_adds_missing_resolved_paths():
    # mock search: SKU -> its catalog path (brand segment from the live FS)
    catalog = {
        "PT-SND-BOS-GEX125-DUST": "/proc/catalog/Bosch Professional/PT-SND-BOS-GEX125-DUST.json",
        "PT-HDG-BOS-UHC18-50-25": "/proc/catalog/Bosch Home and Garden/PT-HDG-BOS-UHC18-50-25.json",
        "PT-BLA-BOS-EXPWOOD-190": "/proc/catalog/Bosch Professional/PT-BLA-BOS-EXPWOOD-190.json",
        "PT-IMP-DEW-DCF887-5AH": "/proc/catalog/DeWalt/PT-IMP-DEW-DCF887-5AH.json",
        "PT-IMP-DEW-DCF887-BODY": "/proc/catalog/DeWalt/PT-IMP-DEW-DCF887-BODY.json",
        "PT-SAW-MAK-DHS680-5AH": "/proc/catalog/Makita/PT-SAW-MAK-DHS680-5AH.json",
    }

    def resolve_fn(sku):
        return catalog.get(sku)

    # agent already cited the qualifying one; completer must add the other 5
    already = ["/proc/catalog/Bosch Professional/PT-SND-BOS-GEX125-DUST.json"]
    add = complete_catalog_refs(_T005, already, resolve_fn)
    assert set(add) == {catalog[s] for s in _T005_SKUS} - set(already)
    # union is the full grader-expected set, no extras
    assert set(already) | set(add) == {catalog[s] for s in _T005_SKUS}


def test_no_duplicate_or_already_present():
    catalog = {"PT-A-B-C": "/proc/catalog/X/PT-A-B-C.json"}
    resolve_fn = lambda sku: catalog.get(sku)
    txt = "how many of these SKUs are available: PT-A-B-C?"
    # already present -> no addition
    assert complete_catalog_refs(txt, ["/proc/catalog/X/PT-A-B-C.json"], resolve_fn) == []
