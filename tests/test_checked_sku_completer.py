"""Unit tests for checked_sku_completer — deterministic, no LLM."""
from __future__ import annotations

from bitgn_contest_agent.checked_sku_completer import (
    CheckedSkuResult,
    complete_checked_sku_refs,
)


SEEN = frozenset({
    "/AGENTS.MD",
    "/proc/catalog/electrical/extension_cables/fam_X/ELC-1SPATM3U.json",
    "/proc/catalog/hand_tools/pliers_wrenches/fam_Y/WRC-12345678.json",
})


def _call(**overrides) -> CheckedSkuResult:
    base = dict(
        kind="yes_no_sku",
        message="<NO> Checked SKU: ELC-1SPATM3U. The ip rating mismatches.",
        refs=["/AGENTS.MD"],
        seen_refs=SEEN,
    )
    base.update(overrides)
    return complete_checked_sku_refs(**base)


class TestHardGuards:
    def test_kind_mismatch_is_noop(self):
        r = _call(kind="count_per_store")
        assert not r.fired
        assert r.reason == "kind_mismatch"
        assert r.refs == ["/AGENTS.MD"]

    def test_already_cited_meaningful_ref_is_noop(self):
        r = _call(refs=["/AGENTS.MD", "/proc/catalog/some/path.json"])
        assert not r.fired
        assert r.reason == "already_cited"

    def test_agents_md_only_does_not_count_as_cited(self):
        r = _call(refs=["/AGENTS.MD"])
        assert r.fired
        assert r.added == [
            "/proc/catalog/electrical/extension_cables/fam_X/ELC-1SPATM3U.json"
        ]

    def test_empty_refs_does_not_count_as_cited(self):
        r = _call(refs=[])
        assert r.fired

    def test_no_marker_is_noop(self):
        r = _call(message="<NO> The variant is absent. ip rating mismatch.")
        assert not r.fired
        assert r.reason == "no_checked_sku_marker"

    def test_sku_not_in_seen_refs_is_noop(self):
        r = _call(message="<NO> Checked SKU: ZZZ-99999999.")
        assert not r.fired
        assert r.reason == "sku_not_in_seen_refs"
        assert r.sku_found == "ZZZ-99999999"


class TestParser:
    def test_basic_form(self):
        r = _call()
        assert r.fired
        assert r.sku_found == "ELC-1SPATM3U"

    def test_case_insensitive_marker(self):
        r = _call(message="<NO> checked sku: ELC-1SPATM3U.")
        assert r.fired

    def test_plural_form(self):
        r = _call(message="<NO> Checked SKUs: ELC-1SPATM3U")
        assert r.fired

    def test_hyphen_separator(self):
        r = _call(message="<NO> Checked SKU - ELC-1SPATM3U")
        assert r.fired

    def test_no_separator(self):
        r = _call(message="<NO> Checked SKU ELC-1SPATM3U")
        assert r.fired

    def test_alt_sku_format_matches(self):
        r = _call(
            message="<NO> Checked SKU: WRC-12345678",
            seen_refs=SEEN,
        )
        assert r.fired
        assert r.added == [
            "/proc/catalog/hand_tools/pliers_wrenches/fam_Y/WRC-12345678.json"
        ]


class TestNeverAddOutsideSeenRefs:
    def test_sku_in_message_but_only_path_in_other_dir(self):
        # SKU is in the message but seen_refs has a different path.
        # Without an endswith match, we don't add.
        r = _call(
            message="<NO> Checked SKU: ABC-99999999",
            seen_refs=frozenset({"/AGENTS.MD", "/proc/catalog/other/XYZ-11111111.json"}),
        )
        assert not r.fired

    def test_endswith_with_leading_slash_is_strict(self):
        # /XELC-1SPATM3U.json must NOT match needle /ELC-1SPATM3U.json
        # because the needle is anchored with a leading slash —
        # `XELC` has no slash before its E, so endswith is safe.
        r = _call(
            message="<NO> Checked SKU: ELC-1SPATM3U",
            seen_refs=frozenset({"/AGENTS.MD", "/proc/catalog/X/XELC-1SPATM3U.json"}),
        )
        assert not r.fired
        assert r.reason == "sku_not_in_seen_refs"
