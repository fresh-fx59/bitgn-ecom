"""Regex extractors that turn grader score_detail strings into rules.

This module is shared by:
  - Phase 1.5 (mining existing PROD JSONL traces + server logs)
  - Phase 2  (parsing live probe responses)

It MUST stay pure: no I/O, no DB, no network. Add new patterns by
appending to PATTERNS. Each pattern is a (regex, rule_kind) pair.
The regex's first capture group becomes the rule_value; if there is
a second capture group, see the per-pattern handling in extract_rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern


@dataclass(frozen=True)
class ExtractedRule:
    rule_kind: str
    rule_value: str


_QUOTE = r"['\"]"

# Patterns ordered by specificity. Each entry compiles to a finditer
# call on the full score_detail string so that concatenated detail
# entries (e.g. "missing X / missing Y") yield multiple rules.
_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(rf"answer is incorrect\. Expected:\s*{_QUOTE}([^'\"]+){_QUOTE}"), "expected_answer"),
    (re.compile(rf"missing file write\s*{_QUOTE}([^'\"]+){_QUOTE}"), "required_write"),
    (re.compile(rf"answer missing required reference\s*{_QUOTE}([^'\"]+){_QUOTE}"), "required_ref"),
    (re.compile(r"expected outcome\s+(\w+),\s*got\s+(\w+)"), "expected_outcome"),
]


def extract_rules(score_detail: str) -> list[ExtractedRule]:
    """Run every pattern across the input string and collect all matches."""
    out: list[ExtractedRule] = []
    for pattern, kind in _PATTERNS:
        for m in pattern.finditer(score_detail):
            value = m.group(1)
            out.append(ExtractedRule(rule_kind=kind, rule_value=value))
    return out


# --- Orchestration ---

import argparse
import os
import re
import sys
from pathlib import Path

from bitgn_scraper.db import insert_scoring_rule
from bitgn_scraper.seed_outcomes import mine_outcomes_dir
from bitgn_scraper.seed_server_logs import mine_server_log

_TASK_ID_RE = re.compile(r"\b(t\d{3})\b")
_DEFAULT_DB = Path("artifacts/harness_db/bitgn_local.db")
_DEFAULT_JSONL_ROOT = Path("logs/prod_cf90740_full")
_DEFAULT_SERVER_LOGS = [
    Path("vm-03owny32f4y68f9cda.eu.bitgn.com.txt"),
    Path("vm-03owny3353zxxh4fm7.eu.bitgn.com.txt"),
    Path("vm-03ox0hre13aqu0pme3.eu.bitgn.com.txt"),
    Path("vm-03ox0hreyjfinmhrvo.eu.bitgn.com.txt"),
    Path("other-prod-run.txt"),
]


def _task_id_from_path(p: Path) -> str | None:
    m = _TASK_ID_RE.search(p.name)
    return m.group(1) if m else None


def seed_from_outcomes(*, db_path: Path, jsonl_root: Path) -> int:
    """Mine JSONL outcome events, write confidence='high' rules, return count."""
    written = 0
    for finding in mine_outcomes_dir(jsonl_root):
        for detail in finding.score_detail:
            for rule in extract_rules(detail):
                insert_scoring_rule(
                    db_path,
                    task_id=finding.task_id,
                    instantiation_hash="",  # unknown — pre-Phase-1 seed
                    rule_kind=rule.rule_kind,
                    rule_value=rule.rule_value,
                    confidence="high",
                    derived_from=None,
                    notes=f"seeded from outcome JSONL: {finding.source_path}",
                )
                written += 1
    return written


def seed_from_server_logs(*, db_path: Path, log_paths: list[Path]) -> int:
    """Mine server-side .log files, write confidence='high' rules, return count."""
    written = 0
    for path in log_paths:
        if not path.exists():
            continue
        task_id = _task_id_from_path(path) or ""
        for finding in mine_server_log(path):
            for rule in extract_rules(finding.score_detail):
                insert_scoring_rule(
                    db_path,
                    task_id=task_id,
                    instantiation_hash="",
                    rule_kind=rule.rule_kind,
                    rule_value=rule.rule_value,
                    confidence="high",
                    derived_from=None,
                    notes=f"seeded from server log: {finding.source_path}",
                )
                written += 1
    return written


def run_seed_cli() -> int:
    """Entry point invoked by scripts/bitgn_scraper.py seed."""
    parser = argparse.ArgumentParser(prog="bitgn_scraper seed")
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--jsonl-root", type=Path, default=_DEFAULT_JSONL_ROOT)
    args = parser.parse_args(sys.argv[2:])  # skip "scripts/.. seed"

    from bitgn_scraper.db import init_schema
    init_schema(args.db)

    n_jsonl = seed_from_outcomes(db_path=args.db, jsonl_root=args.jsonl_root)
    n_logs = seed_from_server_logs(db_path=args.db, log_paths=_DEFAULT_SERVER_LOGS)

    print(f"seed: wrote {n_jsonl} rules from JSONL outcomes")
    print(f"seed: wrote {n_logs} rules from server logs")
    print(f"seed: db at {args.db}")
    return 0
