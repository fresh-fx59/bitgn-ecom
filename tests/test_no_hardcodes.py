"""Policy check: bitgn skill bodies must not reference hardcoded file
paths beyond discovery starting points, must not mention proper-noun
entity names that should be captured at runtime, and must not
contradict the base prompt.

Spec §7.1, §7.3.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bitgn_contest_agent.skill_loader import load_skill


SKILLS_DIR = Path(__file__).parent.parent / "src" / "bitgn_contest_agent" / "skills"


# Disallowed: specific filenames with non-generic basenames.
_HARDCODED_FILENAME_RE = re.compile(
    r"`[^`\s]+\.(?:md|txt|yaml|yml|json|py)`"
)

# Disallowed: proper-noun entity names that should come from the task.
_KNOWN_ENTITY_NAMES = (
    "NORA",
    "DORA",
    "Foundry",
    "Priya",
    "Fuchs",
    "Miriam",
    "Helios",
)

# Disallowed contradiction phrases.
_CONTRADICTION_PATTERNS = (
    r"\bignore\b.*\bsystem prompt\b",
    r"\boverride\b.*\bsystem prompt\b",
    r"\bdo not follow\b",
    r"\binstead of the system prompt\b",
)


def _iter_skills():
    if not SKILLS_DIR.exists():
        return []
    return sorted(SKILLS_DIR.glob("*.md"))


@pytest.mark.parametrize("skill_path", _iter_skills(), ids=lambda p: p.name)
def test_skill_body_has_no_hardcoded_filenames(skill_path: Path) -> None:
    skill = load_skill(skill_path)
    matches = _HARDCODED_FILENAME_RE.findall(skill.body)
    allowed = {"`AGENTS.md`", "`README.md`"}
    offenders = [m for m in matches if m not in allowed]
    assert not offenders, (
        f"{skill_path.name} body references hardcoded filenames "
        f"(spec §7.1): {offenders}. Use discovery starting points "
        f"instead (e.g., `99_system/workflows/` directory)."
    )


@pytest.mark.parametrize("skill_path", _iter_skills(), ids=lambda p: p.name)
def test_skill_body_has_no_hardcoded_entity_names(skill_path: Path) -> None:
    skill = load_skill(skill_path)
    offenders = [n for n in _KNOWN_ENTITY_NAMES if n in skill.body]
    assert not offenders, (
        f"{skill_path.name} body hardcodes entity names "
        f"(spec §7.1): {offenders}. Capture these via matcher_patterns "
        f"variables or discover at runtime from the task text."
    )


@pytest.mark.parametrize("skill_path", _iter_skills(), ids=lambda p: p.name)
def test_skill_body_does_not_contradict_base_prompt(skill_path: Path) -> None:
    skill = load_skill(skill_path)
    body_lower = skill.body.lower()
    offenders = [p for p in _CONTRADICTION_PATTERNS if re.search(p, body_lower)]
    assert not offenders, (
        f"{skill_path.name} body contains language that contradicts the "
        f"base prompt (spec §7.3): {offenders}"
    )
