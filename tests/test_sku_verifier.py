"""Tests for the SKU attribute verifier."""
from __future__ import annotations

import json

from dataclasses import dataclass, field

from bitgn_contest_agent.sku_verifier import (
    FilterResult,
    filter_sku_refs,
    sku_mismatches_task,
    sku_mismatches_spec,
    _normalize,
)


@dataclass
class _Prod:
    brand: str = ""
    series: str = ""
    model: str = ""
    name: str = ""
    attributes: dict = field(default_factory=dict)


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


def test_series_name_token_is_not_an_attribute_spec():
    """v-2026-05 t01 regression: a property whose NAME coincides with a
    token in the series/line name must NOT be treated as a
    task-specified attribute. The task only required storage_type=parts
    case; the product's stackable='yes' (matched only because the
    series is "Stackable") must not strip the correct SKU.
    """
    sku = _sku(
        "Festool",
        "Stackable",
        "SYS 3JJ-9LM",
        storage_type="parts case",
        stackable="yes",
        color_family="yellow",
        volume_l="60",
    )
    task = (
        "Is the Tool Box and Bag from Festool in the Festool Stackable "
        "SYS 3JJ-9LM Tool Box and Bag line that has storage type parts "
        "case in the catalogue?"
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


# ── spec-based path (task_spec.products) ──────────────────────────────


def _sku_full(path, brand, series, model, **props):
    return path, {
        "brand": brand, "series": series, "model": model,
        "name": "x", "properties": dict(props),
    }


def test_spec_keeps_correct_sku_with_extra_props():
    """t01: only storage_type specified; stackable='yes' must not strip."""
    prod = _Prod(brand="Festool", series="Stackable", model="SYS 3JJ-9LM",
                 attributes={"storage_type": "parts case"})
    sku = {"brand": "Festool", "series": "Stackable", "model": "SYS 3JJ-9LM",
           "properties": {"storage_type": "parts case", "stackable": "yes",
                          "color_family": "yellow"}}
    assert sku_mismatches_spec(sku, prod) is None


def test_spec_strips_genuine_attr_mismatch():
    prod = _Prod(brand="Acmetool", series="Pro Z9", model="Z9-DR1",
                 attributes={"voltage": "18 V"})
    sku = {"brand": "Acmetool", "series": "Pro Z9", "model": "Z9-DR1",
           "properties": {"voltage": "12 V"}}
    msg = sku_mismatches_spec(sku, prod)
    assert msg is not None and "voltage" in msg


def test_filter_spec_no_cross_product_contamination():
    """t16: P4's SKU (ip_rating=IP65) must survive even though a
    DIFFERENT product P1 specified ip_rating=IP20."""
    p1 = _Prod(brand="Legrand", series="Indoor", model="LG 3KD-GSH",
               attributes={"device_type": "switch", "color_family": "White",
                           "ip_rating": "IP20"})
    p4 = _Prod(brand="Legrand", series="Outdoor Valena", model="2T3-OA7",
               attributes={"color_family": "White", "length": "2 m"})
    path, sku = _sku_full(
        "/proc/catalog/electrical/ELC-2CE5QWCH.json",
        "Legrand", "Outdoor Valena", "2T3-OA7",
        color_family="White", length="2 m", ip_rating="IP65",
    )
    res = filter_sku_refs(
        task_text="legrand ... ip rating ip20 ... legrand outdoor valena 2t3-oa7 white 2 m",
        refs=[path],
        read_sku=lambda p: json.dumps(sku),
        spec_products=[p1, p4],
    )
    assert res.dropped == [], res.reasons
    assert path in res.kept
