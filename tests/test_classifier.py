"""Unit tests for the shared classifier module."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bitgn_contest_agent.classifier import (
    ClassificationResult,
    _strip_markdown_fences,
    classify_structured,
    parse_response,
)


class TestStripMarkdownFences:
    def test_strips_json_fences(self) -> None:
        text = '```json\n{"category": "X", "confidence": 0.9}\n```'
        assert _strip_markdown_fences(text) == '{"category": "X", "confidence": 0.9}'

    def test_strips_bare_fences(self) -> None:
        text = '```\n{"category": "X"}\n```'
        assert _strip_markdown_fences(text) == '{"category": "X"}'

    def test_passthrough_plain_json(self) -> None:
        text = '{"category": "X", "confidence": 0.9}'
        assert _strip_markdown_fences(text) == '{"category": "X", "confidence": 0.9}'

    def test_strips_whitespace(self) -> None:
        text = '  {"category": "X"}  '
        assert _strip_markdown_fences(text) == '{"category": "X"}'


class TestParseResponse:
    def test_valid_response(self) -> None:
        cat, conf = parse_response(
            {"category": "FOO", "confidence": 0.85},
            valid_categories={"FOO", "BAR"},
        )
        assert cat == "FOO"
        assert conf == 0.85

    def test_unknown_category(self) -> None:
        cat, conf = parse_response(
            {"category": "NOPE", "confidence": 0.9},
            valid_categories={"FOO"},
        )
        assert cat is None
        assert conf == 0.9

    def test_non_dict_returns_none(self) -> None:
        cat, conf = parse_response("not a dict", valid_categories={"FOO"})
        assert cat is None
        assert conf == 0.0

    def test_missing_confidence_defaults_zero(self) -> None:
        cat, conf = parse_response(
            {"category": "FOO"},
            valid_categories={"FOO"},
        )
        assert cat == "FOO"
        assert conf == 0.0


class TestClassifyStructured:
    def test_returns_dict_from_backend(self) -> None:
        """classify_structured delegates to backend.call_structured and
        returns a plain dict compatible with parse_response."""
        mock_backend = MagicMock()
        mock_backend.call_structured.return_value = ClassificationResult(
            category="FINANCE", confidence=0.92,
        )
        result = classify_structured(
            mock_backend, system="classify this", user="some text",
        )
        assert result == {"category": "FINANCE", "confidence": 0.92}
        mock_backend.call_structured.assert_called_once()
        # Verify schema type passed to backend
        call_args = mock_backend.call_structured.call_args
        assert call_args[0][1] is ClassificationResult

    def test_result_works_with_parse_response(self) -> None:
        """End-to-end: classify_structured → parse_response."""
        mock_backend = MagicMock()
        mock_backend.call_structured.return_value = ClassificationResult(
            category="INBOX", confidence=0.85,
        )
        raw = classify_structured(
            mock_backend, system="sys", user="usr",
        )
        cat, conf = parse_response(raw, valid_categories={"INBOX", "OTHER"})
        assert cat == "INBOX"
        assert conf == 0.85
