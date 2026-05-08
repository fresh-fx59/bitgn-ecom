"""Unit tests for the format validation module."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.format_validator import validate_yaml_frontmatter, ValidationResult


class TestValidateYamlFrontmatter:
    def test_valid_frontmatter_passes(self) -> None:
        content = (
            "---\n"
            "record_type: outbound_email\n"
            "subject: Hello world\n"
            "---\n"
            "Body text here.\n"
        )
        result = validate_yaml_frontmatter(content)
        assert result.ok is True
        assert result.error is None

    def test_unquoted_colon_in_value_fails(self) -> None:
        content = (
            "---\n"
            "record_type: outbound_email\n"
            "subject: Re: Invoice request\n"
            "---\n"
            "Body text.\n"
        )
        result = validate_yaml_frontmatter(content)
        assert result.ok is False
        assert result.error is not None
        assert result.line is not None
        assert "subject" in result.error.lower() or "mapping" in result.error.lower()

    def test_no_frontmatter_returns_ok(self) -> None:
        content = "Just plain text, no frontmatter."
        result = validate_yaml_frontmatter(content)
        assert result.ok is True

    def test_unclosed_frontmatter_returns_ok(self) -> None:
        content = "---\nkey: value\nno closing delimiter"
        result = validate_yaml_frontmatter(content)
        assert result.ok is True  # not valid frontmatter structure, skip

    def test_valid_quoted_colon_passes(self) -> None:
        content = (
            "---\n"
            "subject: \"Re: Invoice request\"\n"
            "---\n"
            "Body.\n"
        )
        result = validate_yaml_frontmatter(content)
        assert result.ok is True

    def test_invalid_yaml_syntax_reports_line(self) -> None:
        content = (
            "---\n"
            "key1: value1\n"
            "key2: value2\n"
            "bad line without colon\n"
            "---\n"
            "Body.\n"
        )
        result = validate_yaml_frontmatter(content)
        assert result.ok is False
        assert result.line is not None

    def test_empty_frontmatter_passes(self) -> None:
        content = "---\n---\nBody.\n"
        result = validate_yaml_frontmatter(content)
        assert result.ok is True

    def test_list_values_pass(self) -> None:
        content = (
            "---\n"
            "to:\n"
            "  - alice@example.com\n"
            "  - bob@example.com\n"
            "attachments:\n"
            "  - /path/to/file.md\n"
            "---\n"
            "Body.\n"
        )
        result = validate_yaml_frontmatter(content)
        assert result.ok is True
