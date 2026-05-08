"""Smoke tests for the scraper CLI shell.

Verifies argparse plumbing only — no real subcommand work. The actual
phase0 / seed subcommands are tested in their own modules.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(*argv: str, expect_rc: int) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "scripts/bitgn_scraper.py", *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == expect_rc, (
        f"argv={argv!r} rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return proc


def test_help_lists_subcommands() -> None:
    proc = _run("--help", expect_rc=0)
    out = proc.stdout
    assert "phase0" in out
    assert "seed" in out


def test_no_subcommand_exits_nonzero() -> None:
    _run(expect_rc=2)


def test_unknown_subcommand_exits_nonzero() -> None:
    _run("nope", expect_rc=2)
