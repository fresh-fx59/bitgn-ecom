"""Validate CLI parser smoke test."""
from __future__ import annotations

from pathlib import Path

from bitgn_scraper.validate_cli import build_parser


def test_validate_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.db_path == Path("artifacts/harness_db/bitgn_local.db")
    assert args.workspace_root == Path("artifacts/harness_db/workspaces")
    assert args.skip_determinism is False
    assert args.determinism_samples == 5
