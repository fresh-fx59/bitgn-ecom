"""score_detail string parsing tests.

Fixture strings copied verbatim from real PROD logs:
  - cf90740 22LAfu4 t000 outcome trace
  - cf90740 22LAfu4 t066 outcome trace
  - vm-03owny32f4y68f9cda.eu.bitgn.com.txt server log
"""
from __future__ import annotations

from bitgn_scraper.seed_rules import ExtractedRule, extract_rules


def test_extracts_expected_answer() -> None:
    rules = extract_rules("answer is incorrect. Expected: '1989-02-16'")
    assert rules == [ExtractedRule(rule_kind="expected_answer", rule_value="1989-02-16")]


def test_extracts_required_write() -> None:
    rules = extract_rules(
        "missing file write '50_finance/purchases/2026_01_31__eur_000050__bill__hearthline_sensor_bundle.md'"
    )
    assert rules == [ExtractedRule(
        rule_kind="required_write",
        rule_value="50_finance/purchases/2026_01_31__eur_000050__bill__hearthline_sensor_bundle.md",
    )]


def test_extracts_required_ref() -> None:
    rules = extract_rules(
        "answer missing required reference '20_projects/cabin/plan.md'"
    )
    assert rules == [ExtractedRule(
        rule_kind="required_ref",
        rule_value="20_projects/cabin/plan.md",
    )]


def test_extracts_expected_outcome() -> None:
    rules = extract_rules(
        "expected outcome OUTCOME_OK, got OUTCOME_NONE_CLARIFICATION"
    )
    assert rules == [ExtractedRule(
        rule_kind="expected_outcome",
        rule_value="OUTCOME_OK",
    )]


def test_extracts_multiple_rules_from_one_string() -> None:
    """t066 had concatenated missing-write strings in one detail entry."""
    rules = extract_rules(
        "missing file write '50_finance/purchases/A.md' / "
        "missing file write '50_finance/purchases/B.md'"
    )
    assert ExtractedRule(rule_kind="required_write", rule_value="50_finance/purchases/A.md") in rules
    assert ExtractedRule(rule_kind="required_write", rule_value="50_finance/purchases/B.md") in rules
    assert len(rules) == 2


def test_returns_empty_for_unrecognized_string() -> None:
    rules = extract_rules("the agent panicked")
    assert rules == []


def test_handles_double_quotes_variant() -> None:
    """Some log lines use double quotes instead of single."""
    rules = extract_rules('answer is incorrect. Expected: "1989-02-16"')
    assert rules == [ExtractedRule(rule_kind="expected_answer", rule_value="1989-02-16")]


# --- Orchestration tests (Task 7) ---

import json
from pathlib import Path

import pytest

from bitgn_scraper.db import init_schema, connect
from bitgn_scraper.seed_rules import seed_from_outcomes, seed_from_server_logs


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_schema(p)
    return p


def test_seed_from_outcomes_writes_rules(tmp_path: Path, db: Path) -> None:
    fixture = tmp_path / "t000.jsonl"
    fixture.write_text(
        '{"kind":"meta","task_id":"t000","intent_head":"x","benchmark":"bitgn/pac1-prod"}\n'
        '{"kind":"outcome","score":0.0,"score_detail":["answer is incorrect. Expected: \'1989-02-16\'"]}\n'
    )
    n_rules = seed_from_outcomes(db_path=db, jsonl_root=tmp_path)
    assert n_rules == 1
    with connect(db) as conn:
        row = conn.execute(
            "SELECT task_id, rule_kind, rule_value, confidence "
            "FROM scoring_rules"
        ).fetchone()
    assert row == ("t000", "expected_answer", "1989-02-16", "high")


def test_seed_from_server_logs_attributes_by_filename(tmp_path: Path, db: Path) -> None:
    """Filename convention: `t<NN>-*.log` → task_id=t<NN>."""
    log = tmp_path / "t030-202604131034.log"
    log.write_text(
        "[ ERR ] AI agent score 0.00 / answer is incorrect. Expected: '3D-Druck PLA-Filament 1.75mm'\n"
    )
    n_rules = seed_from_server_logs(db_path=db, log_paths=[log])
    assert n_rules == 1
    with connect(db) as conn:
        row = conn.execute(
            "SELECT task_id, rule_kind, rule_value FROM scoring_rules"
        ).fetchone()
    assert row[0] == "t030"
    assert row[1] == "expected_answer"


def test_seed_skips_unparseable_detail(tmp_path: Path, db: Path) -> None:
    """If extract_rules returns empty, no rows are written."""
    fixture = tmp_path / "t111.jsonl"
    fixture.write_text(
        '{"kind":"meta","task_id":"t111","intent_head":"x","benchmark":"b"}\n'
        '{"kind":"outcome","score":0.0,"score_detail":["unparseable garbage"]}\n'
    )
    n_rules = seed_from_outcomes(db_path=db, jsonl_root=tmp_path)
    assert n_rules == 0
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM scoring_rules").fetchone()[0]
    assert n == 0
