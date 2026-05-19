"""Tests for the SKU attribute verifier."""
from __future__ import annotations

import json

from bitgn_contest_agent.sku_verifier import (
    FilterResult,
    filter_sku_refs,
    sku_mismatches_task,
    _normalize,
)


# ── unit tests for sku_mismatches_task ────────────────────────────────


def _sku(brand, series, model, **props):
    return {
        "brand": brand,
        "series": series,
        "model": model,
        "name": "ignored",
        "properties": dict(props),
    }


def test_matching_sku_returns_none():
    sku = _sku(
        "Acmetool",
        "Acmetool Pro Z9",
        "Z9-DR1",
        voltage="18 V",
        battery_platform="18v-system",
        kit_contents="case",
    )
    task = (
        "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9 "
        "Z9-DR1 Cordless Drill Driver line that has voltage 18 V, "
        "battery platform 18v-system, and kit contents case"
    )
    assert sku_mismatches_task(sku, _normalize(task)) is None


def test_wrong_voltage_is_mismatch():
    sku = _sku(
        "Acmetool",
        "Acmetool Pro Z9",
        "Z9-DR1",
        voltage="12 V",
        battery_platform="12v-system",
        kit_contents="case",
    )
    task = (
        "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9 "
        "Z9-DR1 Cordless Drill Driver line that has voltage 18 V, "
        "battery platform 18v-system, and kit contents case"
    )
    msg = sku_mismatches_task(sku, _normalize(task))
    assert msg is not None
    assert "voltage" in msg


def test_wrong_kit_contents_is_mismatch():
    sku = _sku(
        "Acmetool",
        "Acmetool Pro Z9",
        "Z9-DR1",
        voltage="18 V",
        battery_platform="18v-system",
        kit_contents="bare tool",
    )
    task = (
        "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9 "
        "Z9-DR1 Cordless Drill Driver line that has voltage 18 V, "
        "battery platform 18v-system, and kit contents case"
    )
    msg = sku_mismatches_task(sku, _normalize(task))
    assert msg is not None
    assert "kit_contents" in msg


def test_different_brand_abstain():
    """Brand not in task -> abstain (no mismatch claim)."""
    sku = _sku(
        "OtherBrand",
        "Other Series",
        "OB-1",
        voltage="12 V",
    )
    task = "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9"
    assert sku_mismatches_task(sku, _normalize(task)) is None


def test_different_series_abstain():
    sku = _sku(
        "Acmetool",
        "Acmetool Eco Y3",  # different series
        "Y3-1",
        voltage="12 V",
    )
    task = "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9 line"
    assert sku_mismatches_task(sku, _normalize(task)) is None


def test_task_does_not_mention_property_skip():
    """If task doesn't mention 'voltage', don't check voltage value."""
    sku = _sku(
        "Acmetool",
        "Acmetool Pro Z9",
        "Z9-DR1",
        voltage="999 V",
    )
    task = (
        "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9 "
        "Z9-DR1 line that has kit contents case"
    )
    assert sku_mismatches_task(sku, _normalize(task)) is None


def test_no_properties_dict():
    sku = {"brand": "Acmetool", "series": "Acmetool Pro Z9", "model": "Z9"}
    task = "Acmetool Pro Z9 with voltage 18 V"
    assert sku_mismatches_task(sku, _normalize(task)) is None


# ── filter_sku_refs integration ────────────────────────────────────────


def test_filter_drops_wrong_attribute_sku():
    catalog = {
        "/proc/catalog/Acmetool/PWR-RIGHT.json": json.dumps(
            _sku(
                "Acmetool",
                "Acmetool Pro Z9",
                "Z9-DR1",
                voltage="18 V",
                kit_contents="case",
            )
        ),
        "/proc/catalog/Acmetool/PWR-LOW.json": json.dumps(
            _sku(
                "Acmetool",
                "Acmetool Pro Z9",
                "Z9-DR1",
                voltage="12 V",
                kit_contents="case",
            )
        ),
    }
    task = (
        "the Cordless Drill Driver from Acmetool in the Acmetool Pro Z9 "
        "Z9-DR1 line that has voltage 18 V and kit contents case"
    )
    res = filter_sku_refs(
        task_text=task,
        refs=list(catalog) + ["/proc/stores/store_x.json"],
        read_sku=lambda p: catalog.get(p),
    )
    assert "/proc/catalog/Acmetool/PWR-RIGHT.json" in res.kept
    assert "/proc/stores/store_x.json" in res.kept  # non-catalog passthrough
    assert "/proc/catalog/Acmetool/PWR-LOW.json" in res.dropped


def test_filter_preserves_non_catalog_refs():
    res = filter_sku_refs(
        task_text="any task",
        refs=["/AGENTS.MD", "/docs/security.md", "/proc/stores/store_x.json"],
        read_sku=lambda p: None,
    )
    assert res.dropped == []
    assert len(res.kept) == 3


def test_filter_empty_task_text_noop():
    catalog = {
        "/proc/catalog/X/Y.json": json.dumps(
            _sku("X", "Series A", "A-1", voltage="18 V"),
        ),
    }
    res = filter_sku_refs(
        task_text="",
        refs=list(catalog),
        read_sku=lambda p: catalog.get(p),
    )
    assert res.dropped == []


def test_filter_read_failure_keeps_ref():
    res = filter_sku_refs(
        task_text="Acmetool Pro Z9 voltage 18 V",
        refs=["/proc/catalog/Acmetool/PWR-X.json"],
        read_sku=lambda p: None,
    )
    assert res.kept == ["/proc/catalog/Acmetool/PWR-X.json"]


def test_filter_invalid_json_keeps_ref():
    res = filter_sku_refs(
        task_text="Acmetool Pro Z9 voltage 18 V",
        refs=["/proc/catalog/Acmetool/PWR-X.json"],
        read_sku=lambda p: "not-json{",
    )
    assert res.kept == ["/proc/catalog/Acmetool/PWR-X.json"]


def test_filter_real_v160b_t14_overcite_pattern():
    """Reconstruct the v0.1.60-b t14 failure: 6 candidates, only 2
    qualify. The wrong-voltage Acmetool variant should be dropped."""
    catalog = {
        "/proc/catalog/Acmetool/PWR-P1RIGHT.json": json.dumps(
            _sku(
                "Acmetool",
                "Acmetool Pro Z9",
                "Z9-DR1",
                voltage="18 V",
                battery_platform="18v-system",
                kit_contents="case",
            )
        ),
        "/proc/catalog/Acmetool/PWR-P1LOW.json": json.dumps(
            _sku(
                "Acmetool",
                "Acmetool Pro Z9",
                "Z9-DR1",
                voltage="12 V",
                battery_platform="12v-system",
                kit_contents="case",
            )
        ),
        "/proc/catalog/Acmetool/PWR-P1BARE.json": json.dumps(
            _sku(
                "Acmetool",
                "Acmetool Pro Z9",
                "Z9-DR1",
                voltage="18 V",
                battery_platform="18v-system",
                kit_contents="bare tool",
            )
        ),
    }
    task = (
        "How many of these products have at least 1 items available in "
        "Acmetown Central: the Cordless Drill Driver from Acmetool in "
        "the Acmetool Pro Z9 Z9-DR1 Cordless Drill Driver line that "
        "has voltage 18 V, battery platform 18v-system, and kit "
        "contents case"
    )
    res = filter_sku_refs(
        task_text=task,
        refs=list(catalog),
        read_sku=lambda p: catalog.get(p),
    )
    assert "/proc/catalog/Acmetool/PWR-P1RIGHT.json" in res.kept
    assert "/proc/catalog/Acmetool/PWR-P1LOW.json" in res.dropped
    assert "/proc/catalog/Acmetool/PWR-P1BARE.json" in res.dropped
