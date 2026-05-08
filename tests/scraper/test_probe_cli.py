"""Probe CLI parser smoke test."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.probe_cli import build_parser


def test_probe_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.benchmark_id == "bitgn/pac1-prod"
    assert args.p2b_sample == 0
    assert args.p6_sample == 0
    assert args.db_path == Path("artifacts/harness_db/bitgn_local.db")


def test_probe_cli_parser_diagnostics() -> None:
    parser = build_parser()
    args = parser.parse_args(["--p2b-sample", "10", "--p6-sample", "5"])
    assert args.p2b_sample == 10
    assert args.p6_sample == 5
