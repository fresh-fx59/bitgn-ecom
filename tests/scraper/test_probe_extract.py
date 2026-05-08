"""Probe-output rule extractor tests."""
from __future__ import annotations

from bitgn_scraper.probe_extract import extract_probe_rules


def test_extract_expected_outcome_mismatch() -> None:
    rules = extract_probe_rules(
        "expected outcome OUTCOME_NONE_CLARIFICATION, got OUTCOME_OK"
    )
    kinds = {(r.rule_kind, r.rule_value) for r in rules}
    assert ("expected_outcome", "OUTCOME_NONE_CLARIFICATION") in kinds


def test_extract_answer_must_include() -> None:
    rules = extract_probe_rules(
        "answer must include the date of the project kickoff"
    )
    kinds = {(r.rule_kind, r.rule_value) for r in rules}
    assert ("answer_constraint", "date") in kinds


def test_extract_combines_seed_patterns_and_probe_patterns() -> None:
    # A score_detail line with both an expected-answer pattern (from seed_rules)
    # and a missing-write pattern (also from seed_rules) should produce two rules.
    detail = "answer is incorrect. Expected: '1989-02-16'; missing file write '/work/notes.md'"
    rules = extract_probe_rules(detail)
    kinds = {(r.rule_kind, r.rule_value) for r in rules}
    assert ("expected_answer", "1989-02-16") in kinds
    assert ("required_write", "/work/notes.md") in kinds


def test_extract_unmatched_returns_empty() -> None:
    assert extract_probe_rules("some unrelated string") == []
