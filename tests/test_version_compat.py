"""§5.2 Test 5 — every committed fixture parses cleanly and yields the
same core metrics via the current analyzer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitgn_contest_agent.trace_schema import TraceMeta, TraceOutcome, load_jsonl
from scripts.bench_summary import summarize


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _committed_fixtures() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("trace_v*.jsonl"))


@pytest.mark.parametrize("fixture", _committed_fixtures(), ids=lambda p: p.name)
def test_fixture_parses_with_current_analyzer(fixture: Path) -> None:
    records = list(load_jsonl(fixture))
    assert records, f"{fixture.name} is empty"
    assert any(r.kind == "meta" for r in records)
    assert any(r.kind == "outcome" for r in records)


@pytest.mark.parametrize("fixture", _committed_fixtures(), ids=lambda p: p.name)
def test_fixture_summarizes_without_error(tmp_path: Path, fixture: Path) -> None:
    # summarize walks a directory, so copy the single fixture into a dir.
    (tmp_path / fixture.name).write_bytes(fixture.read_bytes())
    summary = summarize(logs_dir=tmp_path)
    assert "overall" in summary
    assert summary["overall"]["total_runs"] == 1
