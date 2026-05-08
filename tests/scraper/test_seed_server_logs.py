"""Server-log miner tests."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.seed_server_logs import ServerLogFinding, mine_server_log

FIXTURE = Path(__file__).parent / "fixtures" / "sample_server.log"


def test_mine_extracts_score_and_detail() -> None:
    findings = mine_server_log(FIXTURE)
    assert len(findings) == 1
    f = findings[0]
    assert f.score == 0.0
    assert f.score_detail == "answer is incorrect. Expected: '3D-Druck PLA-Filament 1.75mm'"
    assert f.source_path == str(FIXTURE)


def test_mine_returns_empty_when_no_score_line(tmp_path: Path) -> None:
    p = tmp_path / "noscore.log"
    p.write_text("[ INFO ] nothing here\n")
    assert mine_server_log(p) == []


def test_mine_handles_passing_score_line(tmp_path: Path) -> None:
    """`AI agent score 1.00` → no findings (we only seed from failures)."""
    p = tmp_path / "pass.log"
    p.write_text("[ ERR ] AI agent score 1.00 / great job\n")
    assert mine_server_log(p) == []
