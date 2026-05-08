"""Bitgn skill file parser.

A bitgn skill file is a markdown document with YAML-style frontmatter
delimited by `---` lines. The loader extracts frontmatter into a
typed dataclass and returns the body as raw markdown.

Design rules (spec §5.5):
- Frontmatter is a restricted YAML subset. We do NOT pull in PyYAML
  because spec §2 forbids new runtime deps. The loader implements a
  narrow line-level parser that handles only the keys the bitgn
  skill format uses: string scalars and simple list-of-string blocks
  under `matcher_patterns` / `variables`.
- Required keys: name, description, type, category, matcher_patterns.
- Optional keys: variables.
- type MUST be one of `rigid` | `flexible`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


class SkillFormatError(ValueError):
    """Raised when a bitgn skill file fails the format contract."""


@dataclass(frozen=True, slots=True)
class BitgnSkill:
    name: str
    description: str
    type: str  # "rigid" | "flexible"
    category: str
    matcher_patterns: List[str]
    body: str
    variables: List[str] = field(default_factory=list)
    classifier_hint: Optional[str] = None


_REQUIRED_KEYS = ("name", "description", "type", "category", "matcher_patterns")
_VALID_TYPES = ("rigid", "flexible")


def load_skill(path: Path) -> BitgnSkill:
    """Parse a bitgn skill file and return a BitgnSkill.

    Raises SkillFormatError on any format violation.
    """
    text = Path(path).read_text(encoding="utf-8")
    frontmatter_text, body = _split_frontmatter(text, path)
    parsed = _parse_frontmatter(frontmatter_text, path)
    _validate(parsed, path)
    return BitgnSkill(
        name=parsed["name"],
        description=parsed["description"],
        type=parsed["type"],
        category=parsed["category"],
        matcher_patterns=list(parsed["matcher_patterns"]),
        variables=list(parsed.get("variables", [])),
        classifier_hint=parsed.get("classifier_hint"),
        body=body.strip() + "\n",
    )


def _split_frontmatter(text: str, path: Path) -> Tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError(
            f"{path}: expected `---` on the first line to open frontmatter"
        )
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise SkillFormatError(
        f"{path}: missing closing `---` delimiter for frontmatter"
    )


def _parse_frontmatter(text: str, path: Path) -> dict:
    """Narrow line-level YAML subset parser.

    Accepts:
        key: value           # string scalar
        key:                 # list introduction
          - item1            # list entry (2-space indent)
          - item2
    """
    result: dict = {}
    current_list_key: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            current_list_key = None
            continue
        if raw.startswith("  - "):
            if current_list_key is None:
                raise SkillFormatError(
                    f"{path}: list item `{raw}` has no parent key"
                )
            result.setdefault(current_list_key, []).append(
                _unquote(raw[4:].strip())
            )
            continue
        if ":" not in raw:
            raise SkillFormatError(f"{path}: malformed frontmatter line `{raw}`")
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            # list introduction
            current_list_key = key
            result[key] = []
        else:
            current_list_key = None
            result[key] = _unquote(value)
    return result


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _validate(parsed: dict, path: Path) -> None:
    for key in _REQUIRED_KEYS:
        if key not in parsed:
            raise SkillFormatError(
                f"{path}: missing required frontmatter key `{key}`"
            )
    if parsed["type"] not in _VALID_TYPES:
        raise SkillFormatError(
            f"{path}: type must be one of rigid|flexible, got {parsed['type']!r}"
        )
    patterns = parsed["matcher_patterns"]
    has_hint = bool(parsed.get("classifier_hint"))
    if not isinstance(patterns, list):
        raise SkillFormatError(
            f"{path}: matcher_patterns must be a list"
        )
    if not patterns and not has_hint:
        raise SkillFormatError(
            f"{path}: skill must have matcher_patterns or classifier_hint"
        )
