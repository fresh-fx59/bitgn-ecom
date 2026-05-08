"""Tests for grounding_refs sanitization in envelope salvage.

Evidence: 2026-04-22 gpt-oss-120b PROD run, task t103, emitted
``grounding_refs=["AGENTS.MD", "...bill.md", "5", "5", "", "", ""]``.
Each junk token ("5", "") tripped the validator ("grounding_ref 'X' never
successfully read"), rejecting an otherwise-valid terminal. These tests
lock in that salvage paths filter such tokens while keeping real paths.
"""
from __future__ import annotations

import json

from bitgn_contest_agent.backend.adapters._helpers import (
    _sanitize_grounding_refs,
    try_envelope,
)


def test_sanitize_drops_empty_strings() -> None:
    merged = {"grounding_refs": ["", "   ", "AGENTS.MD"]}
    _sanitize_grounding_refs(merged)
    assert merged["grounding_refs"] == ["AGENTS.MD"]


def test_sanitize_drops_short_non_path_tokens() -> None:
    """'5', 'x', '12' are all observable salvage-junk shapes."""
    merged = {"grounding_refs": ["5", "12", "x", "50_finance/purchases/bill.md"]}
    _sanitize_grounding_refs(merged)
    assert merged["grounding_refs"] == ["50_finance/purchases/bill.md"]


def test_sanitize_keeps_dotted_filenames() -> None:
    """A bare filename with extension is a legitimate ref (AGENTS.md is
    the canonical example)."""
    merged = {"grounding_refs": ["AGENTS.md", "README.MD"]}
    _sanitize_grounding_refs(merged)
    assert merged["grounding_refs"] == ["AGENTS.md", "README.MD"]


def test_sanitize_keeps_slash_paths() -> None:
    merged = {"grounding_refs": ["99_system/AGENTS", "50_finance/purchases/x.md"]}
    _sanitize_grounding_refs(merged)
    assert merged["grounding_refs"] == ["99_system/AGENTS", "50_finance/purchases/x.md"]


def test_sanitize_drops_non_string_entries() -> None:
    merged = {"grounding_refs": ["AGENTS.md", 5, None, {}, "README.md"]}
    _sanitize_grounding_refs(merged)
    assert merged["grounding_refs"] == ["AGENTS.md", "README.md"]


def test_sanitize_t103_observed_case() -> None:
    """Exact shape from 2026-04-22 gpt-oss-120b t103: a mixed array of
    real paths and free-text numeric tokens."""
    merged = {"grounding_refs": [
        "AGENTS.MD",
        "50_finance/purchases/2026_01_04__eur_000080__bill.md",
        "50_finance/purchases/2025_11_12__eur_000072__bill__partial?",
        "5", "5", "", "", "",
    ]}
    _sanitize_grounding_refs(merged)
    # Real paths kept (the `?`-suffixed one still passes the sanitizer;
    # the downstream read-success validator catches it).
    assert "AGENTS.MD" in merged["grounding_refs"]
    assert any("2026_01_04" in r for r in merged["grounding_refs"])
    # All junk dropped.
    assert "5" not in merged["grounding_refs"]
    assert "" not in merged["grounding_refs"]


def test_sanitize_handles_missing_key() -> None:
    merged = {"current_state": "s"}
    _sanitize_grounding_refs(merged)
    assert "grounding_refs" not in merged


def test_sanitize_handles_non_list() -> None:
    """Some models emit a bare string — leave it alone so downstream
    validation raises a clear schema error rather than mutating silently."""
    merged = {"grounding_refs": "AGENTS.md"}
    _sanitize_grounding_refs(merged)
    assert merged["grounding_refs"] == "AGENTS.md"


def test_try_envelope_sanitizes_embedded_refs() -> None:
    """End-to-end: envelope salvage path drops junk refs before validation.

    Shape mirrors GLM/qwen envelope-as-content emissions with a terminal
    report_completion; if the sanitizer didn't run, the ValidationError or
    a downstream grounding_ref check would fire on '5'/''."""
    envelope = {
        "current_state": "done",
        "plan_remaining_steps_brief": [],
        "identity_verified": True,
        "observation": "read files",
        "outcome_leaning": "OUTCOME_OK",
        "function": {
            "tool": "report_completion",
            "message": "241",
            "grounding_refs": ["AGENTS.md", "50_finance/purchases/bill.md", "5", ""],
            "rulebook_notes": "—",
            "outcome_justification": "computed",
            "completed_steps_laconic": ["read bills"],
            "outcome": "OUTCOME_OK",
        },
    }
    ns = try_envelope(json.dumps(envelope))
    assert ns is not None
    assert ns.function.tool == "report_completion"
    # Junk dropped; real refs preserved.
    assert set(ns.function.grounding_refs) == {
        "AGENTS.md",
        "50_finance/purchases/bill.md",
    }
