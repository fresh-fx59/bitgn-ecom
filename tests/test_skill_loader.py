"""Unit tests for skill_loader — bitgn skill frontmatter + body parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from bitgn_contest_agent.skill_loader import (
    BitgnSkill,
    SkillFormatError,
    load_skill,
)


FIX = Path(__file__).parent / "fixtures" / "skills"


def test_load_valid_skill() -> None:
    skill = load_skill(FIX / "valid.md")
    assert isinstance(skill, BitgnSkill)
    assert skill.name == "test-valid"
    assert skill.type == "rigid"
    assert skill.category == "TEST_CATEGORY"
    assert skill.matcher_patterns == ["TEST-ROUTE", r"test (\w+) route"]
    assert skill.variables == ["target_name"]
    assert skill.body.startswith("# Test Valid Skill")
    assert "Emit OUTCOME_OK" in skill.body


def test_load_missing_close_delimiter_raises() -> None:
    with pytest.raises(SkillFormatError, match="closing"):
        load_skill(FIX / "missing_close.md")


def test_load_missing_required_field_raises() -> None:
    with pytest.raises(SkillFormatError, match="name"):
        load_skill(FIX / "missing_required.md")


def test_skill_must_declare_type_or_reject(tmp_path: Path) -> None:
    """A skill without type=rigid|flexible is a spec violation."""
    path = tmp_path / "no_type.md"
    path.write_text(
        "---\n"
        "name: no-type\n"
        "description: has no type field\n"
        "category: FOO\n"
        "matcher_patterns:\n"
        "  - 'foo'\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillFormatError, match="type"):
        load_skill(path)


def test_skill_type_must_be_rigid_or_flexible(tmp_path: Path) -> None:
    path = tmp_path / "bad_type.md"
    path.write_text(
        "---\n"
        "name: bad-type\n"
        "description: has wrong type\n"
        "type: stringent\n"
        "category: FOO\n"
        "matcher_patterns:\n"
        "  - 'foo'\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillFormatError, match="rigid.*flexible|flexible.*rigid"):
        load_skill(path)


def test_empty_matcher_patterns_raises_without_classifier_hint(tmp_path: Path) -> None:
    """Empty patterns without classifier_hint should still error."""
    path = tmp_path / "empty_patterns.md"
    path.write_text(
        "---\n"
        "name: no-patterns\n"
        "description: has empty matcher_patterns\n"
        "type: rigid\n"
        "category: FOO\n"
        "matcher_patterns:\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillFormatError, match="matcher_patterns or classifier_hint"):
        load_skill(path)


def test_empty_matcher_patterns_allowed_with_classifier_hint(tmp_path: Path) -> None:
    """Empty patterns are fine when classifier_hint provides routing."""
    path = tmp_path / "good.md"
    path.write_text(
        "---\n"
        "name: hint-only\n"
        "description: a classifier-routed skill\n"
        "type: rigid\n"
        "category: test_hint\n"
        "matcher_patterns:\n"
        "classifier_hint: Tasks that need hint-only routing\n"
        "---\n"
        "Skill body here.\n",
        encoding="utf-8",
    )
    skill = load_skill(path)
    assert skill.name == "hint-only"
    assert skill.matcher_patterns == []
    assert skill.classifier_hint == "Tasks that need hint-only routing"


def test_body_preserves_markdown_structure() -> None:
    """The body is forwarded to the agent verbatim; the loader must
    not swallow the '## Rule' or '## Process' section headers."""
    skill = load_skill(FIX / "valid.md")
    assert "## Rule" in skill.body
    assert "## Process" in skill.body


def test_variables_are_optional() -> None:
    """A skill can be loaded with or without a variables block; the
    hardcode fixture has no variables field."""
    skill = load_skill(FIX / "body_hardcode.md")
    assert skill.variables == []
    assert skill.matcher_patterns == ["TEST-HARDCODE"]


