"""Post-write format validation for structured documents.

Validates YAML frontmatter in files written by the agent. Uses PyYAML
for deterministic parsing — catches errors that LLMs miss (unquoted
colons, invalid mapping values) and reports exact line/column.

Called automatically by the agent loop after every write tool call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import yaml


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    error: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None


def validate_yaml_frontmatter(content: str) -> ValidationResult:
    """Validate YAML frontmatter in a document.

    Returns ValidationResult.ok=True if:
    - The content has no frontmatter (no opening ``---``)
    - The frontmatter is not properly delimited (no closing ``---``)
    - The frontmatter parses successfully

    Returns ValidationResult.ok=False with error details if the
    frontmatter block exists but fails YAML parsing.
    """
    frontmatter = _extract_frontmatter(content)
    if frontmatter is None:
        return ValidationResult(ok=True)

    try:
        yaml.safe_load(frontmatter)
        return ValidationResult(ok=True)
    except yaml.YAMLError as exc:
        line = None
        column = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line = exc.problem_mark.line + 1  # 0-indexed → 1-indexed
            column = exc.problem_mark.column + 1
        return ValidationResult(
            ok=False,
            error=str(exc),
            line=line,
            column=column,
        )


def _extract_frontmatter(content: str) -> Optional[str]:
    """Extract the YAML frontmatter block between ``---`` delimiters.

    Returns None if content doesn't start with ``---`` or has no
    closing delimiter.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:i])
    return None  # no closing delimiter
