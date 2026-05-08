"""Outcome-JSONL miner tests."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.seed_outcomes import OutcomeFinding, mine_outcomes_dir, mine_outcomes_file

FIXTURE = Path(__file__).parent / "fixtures" / "sample_outcome.jsonl"


def test_mine_single_file_extracts_failed_outcome() -> None:
    findings = mine_outcomes_file(FIXTURE)
    assert len(findings) == 1
    f = findings[0]
    assert f.task_id == "t000"
    assert f.score == 0.0
    assert f.score_detail == ["answer is incorrect. Expected: '1989-02-16'"]
    assert f.intent_head == "When was my partner born? Answer YYYY-MM-DD. Date only"
    assert f.benchmark_id == "bitgn/pac1-prod"
    assert f.source_path == str(FIXTURE)


def test_mine_dir_walks_jsonl_files(tmp_path: Path) -> None:
    # Copy fixture twice with different names
    (tmp_path / "t000.jsonl").write_bytes(FIXTURE.read_bytes())
    (tmp_path / "t001.jsonl").write_bytes(FIXTURE.read_bytes())
    findings = mine_outcomes_dir(tmp_path)
    assert len(findings) == 2


def test_mine_skips_passing_outcomes(tmp_path: Path) -> None:
    passing = (
        '{"kind":"meta","task_id":"t999","intent_head":"x","benchmark":"b"}\n'
        '{"kind":"outcome","score":1.0,"score_detail":null}\n'
    )
    p = tmp_path / "t999.jsonl"
    p.write_text(passing)
    findings = mine_outcomes_file(p)
    assert findings == []


def test_mine_skips_outcomes_without_detail(tmp_path: Path) -> None:
    """Score=0.0 with empty detail can't seed a rule — skip."""
    no_detail = (
        '{"kind":"meta","task_id":"t777","intent_head":"x","benchmark":"b"}\n'
        '{"kind":"outcome","score":0.0,"score_detail":[]}\n'
    )
    p = tmp_path / "t777.jsonl"
    p.write_text(no_detail)
    findings = mine_outcomes_file(p)
    assert findings == []
