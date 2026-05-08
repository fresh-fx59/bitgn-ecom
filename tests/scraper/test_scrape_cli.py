"""Smoke test that the scrape CLI argparses and dispatches."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bitgn_scraper.scrape_cli import build_parser


def test_scrape_cli_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.benchmark_id == "bitgn/pac1-prod"
    assert args.max_attempts == 30
    assert args.saturation_threshold == 5
    assert args.db_path == Path("artifacts/harness_db/bitgn_local.db")


def test_scrape_cli_parser_task_filter() -> None:
    parser = build_parser()
    args = parser.parse_args(["--task-ids", "t001,t002"])
    assert args.task_ids == "t001,t002"
