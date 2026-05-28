"""Unit tests for catalog_strip_enforcer."""
from __future__ import annotations

from bitgn_contest_agent.catalog_strip_enforcer import (
    CatalogStripResult,
    strip_catalog_refs,
)


REFS_WITH_OVERCITE = [
    "/AGENTS.MD",
    "/docs/policy-updates/2024-07-17-catalogue-reporting-tool-boxes-bags-graz.md",
    "/proc/catalog/storage/tool_boxes_bags/STO-1F1H9FYY.json",
    "/proc/catalog/storage/tool_boxes_bags/STO-281RMFUG.json",
    "/proc/catalog/storage/tool_boxes_bags/STO-2F42QXUN.json",
]


class TestTrigger:
    def test_strips_catalog_when_addenda_and_catalogue_count(self):
        r = strip_catalog_refs(kind="catalogue_count", refs=REFS_WITH_OVERCITE)
        assert not r.aborted
        assert len(r.dropped) == 3
        assert "/docs/policy-updates/2024-07-17-catalogue-reporting-tool-boxes-bags-graz.md" in r.refs
        assert "/AGENTS.MD" in r.refs
        assert not any("/proc/catalog/" in p for p in r.refs)

    def test_kind_mismatch_aborts(self):
        r = strip_catalog_refs(kind="count_per_store", refs=REFS_WITH_OVERCITE)
        assert r.aborted
        assert r.abort_reason == "kind_mismatch"
        assert r.refs == REFS_WITH_OVERCITE

    def test_no_addenda_aborts(self):
        # catalogue_count but no addenda cited — agent may genuinely
        # need the catalog refs (no addenda → individual products
        # are the only evidence)
        refs = [
            "/AGENTS.MD",
            "/proc/catalog/storage/tool_boxes_bags/STO-1F1H9FYY.json",
        ]
        r = strip_catalog_refs(kind="catalogue_count", refs=refs)
        assert r.aborted
        assert r.abort_reason == "no_addenda_doc"


class TestAddendaPatterns:
    def test_date_prefix_addenda_recognised(self):
        r = strip_catalog_refs(
            kind="catalogue_count",
            refs=[
                "/docs/catalogue-counts/2025-03-12-screwdriver-hex-sets.md",
                "/proc/catalog/hand_tools/screwdrivers/SCR-1.json",
            ],
        )
        assert not r.aborted
        assert r.dropped == ["/proc/catalog/hand_tools/screwdrivers/SCR-1.json"]

    def test_no_date_but_keyword_addenda_recognised(self):
        r = strip_catalog_refs(
            kind="catalogue_count",
            refs=[
                "/docs/policy-updates/catalogue-reporting-extension-cables.md",
                "/proc/catalog/electrical/extension_cables/ELC-1.json",
            ],
        )
        assert not r.aborted
        assert r.dropped == ["/proc/catalog/electrical/extension_cables/ELC-1.json"]


class TestSafety:
    def test_never_strips_agents_md(self):
        r = strip_catalog_refs(kind="catalogue_count", refs=REFS_WITH_OVERCITE)
        assert "/AGENTS.MD" in r.refs

    def test_never_strips_addenda(self):
        r = strip_catalog_refs(kind="catalogue_count", refs=REFS_WITH_OVERCITE)
        addenda = "/docs/policy-updates/2024-07-17-catalogue-reporting-tool-boxes-bags-graz.md"
        assert addenda in r.refs

    def test_never_strips_stores(self):
        # Stores aren't catalog refs — keep them
        refs = [
            "/docs/policy-updates/2024-07-17-catalogue-reporting-x.md",
            "/proc/stores/store_x.json",
            "/proc/catalog/x/y.json",
        ]
        r = strip_catalog_refs(kind="catalogue_count", refs=refs)
        assert "/proc/stores/store_x.json" in r.refs
        assert r.dropped == ["/proc/catalog/x/y.json"]

    def test_never_strips_payments_returns(self):
        refs = [
            "/docs/policy-updates/2024-07-17-catalogue-reporting-x.md",
            "/proc/payments/pay_001.json",
            "/proc/returns/ret_001.json",
            "/proc/catalog/x/y.json",
        ]
        r = strip_catalog_refs(kind="catalogue_count", refs=refs)
        assert "/proc/payments/pay_001.json" in r.refs
        assert "/proc/returns/ret_001.json" in r.refs

    def test_order_preserved_in_kept(self):
        refs = REFS_WITH_OVERCITE
        r = strip_catalog_refs(kind="catalogue_count", refs=refs)
        # /AGENTS.MD and addenda should stay in original order
        assert r.refs[0] == "/AGENTS.MD"
        assert "policy-updates" in r.refs[1]
