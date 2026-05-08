# src/bitgn_scraper/probe_extract.py
"""Extend seed-rules extractors with probe-specific patterns.

Phase 2 score_detail strings include patterns the seed_rules module
doesn't carry (e.g. "answer must include the X of"). This module
composes seed_rules.extract_rules with the additional regex pass.
"""
from __future__ import annotations

import re

from bitgn_scraper.seed_rules import ExtractedRule, extract_rules

_ANSWER_CONSTRAINT = re.compile(r"answer must include the (\w+) of")


def extract_probe_rules(score_detail: str) -> list[ExtractedRule]:
    """Combine seed-rule patterns + probe-specific patterns."""
    rules = list(extract_rules(score_detail))

    for m in _ANSWER_CONSTRAINT.finditer(score_detail):
        rules.append(ExtractedRule(
            rule_kind="answer_constraint",
            rule_value=m.group(1),
        ))
    return rules
