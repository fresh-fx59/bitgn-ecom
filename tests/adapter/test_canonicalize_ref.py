"""Unit tests for _canonicalize_ref.

Regression cover for the 2026-05-11 PROD failures: t02 cited the
nested ``/proc/catalog/Helios/PNT-169R7W8O.json`` form that
``products.path`` returned, but the grader required the flat
``/proc/catalog/PNT-169R7W8O.json``. Auto-canonicalizing outgoing
refs in submit_terminal fixes the wire format without depending on
the LLM to always pick the flat form.
"""
from __future__ import annotations

import pytest

from bitgn_contest_agent.adapter.ecom import _canonicalize_ref


@pytest.mark.parametrize("inp,expected", [
    # Nested catalog paths normalize to flat.
    (
        "/proc/catalog/Helios/PNT-169R7W8O.json",
        "/proc/catalog/PNT-169R7W8O.json",
    ),
    (
        "/proc/catalog/power_tools/corded_angle_grinder/PWR-1ALYVIXX.json",
        "/proc/catalog/PWR-1ALYVIXX.json",
    ),
    (
        "/proc/catalog/Mobil/AUT-3TE8KXP4.json",
        "/proc/catalog/AUT-3TE8KXP4.json",
    ),
    # Already-flat paths pass through.
    ("/proc/catalog/CLN-GEF2EYP9.json", "/proc/catalog/CLN-GEF2EYP9.json"),
    # Non-catalog paths pass through unchanged.
    ("/AGENTS.MD", "/AGENTS.MD"),
    (
        "/proc/stores/store_vienna_meidling.json",
        "/proc/stores/store_vienna_meidling.json",
    ),
    # Paths that don't fit the SKU shape pass through (no false matches).
    (
        "/proc/catalog/SomeBrand/not-a-sku.json",
        "/proc/catalog/SomeBrand/not-a-sku.json",
    ),
    # Empty string is a no-op (defensive).
    ("", ""),
])
def test_canonicalize_ref(inp: str, expected: str) -> None:
    assert _canonicalize_ref(inp) == expected
