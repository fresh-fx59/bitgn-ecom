"""Unit tests for the shared classifier module."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent import classifier as classifier_module
from bitgn_contest_agent.classifier import (
    ClassificationResult,
    _strip_markdown_fences,
    classify_structured,
    parse_response,
    raw_completion,
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


def _stream_chunks(content_pieces: list[str]):
    """Build an iterable of streaming chunk objects shaped like
    OpenAI SDK streamed responses (each chunk has choices[0].delta.content)."""
    chunks = []
    for piece in content_pieces:
        ch = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))],
        )
        chunks.append(ch)
    return chunks


class TestRawCompletionStreaming:
    """Locks in the v0.1.117 streaming fix: cliproxyapi drops
    `message.content` on non-streaming chat.completions for every
    reasoning model. raw_completion MUST stream + concat deltas
    (commit cd1305f). See project_cliproxyapi_content_drop_bug.md."""

    def test_streams_and_concatenates_deltas(self) -> None:
        chunks = _stream_chunks(['{"hel', 'lo":"', 'world"}'])
        with patch.object(classifier_module, "_llm_call",
                          return_value=iter(chunks)) as mock_llm, \
             patch.object(classifier_module, "_get_openai_client",
                          return_value=MagicMock()):
            out = raw_completion(prompt="anything", system="strict")
        assert out == '{"hello":"world"}'
        # Critically, stream=True must be passed
        kwargs = mock_llm.call_args.kwargs
        assert kwargs.get("stream") is True
        assert "include_usage" in (kwargs.get("stream_options") or {})

    def test_passes_reasoning_effort_in_extra_body(self) -> None:
        with patch.object(classifier_module, "_llm_call",
                          return_value=iter([])) as mock_llm, \
             patch.object(classifier_module, "_get_openai_client",
                          return_value=MagicMock()):
            raw_completion(prompt="x")
        kwargs = mock_llm.call_args.kwargs
        extra = kwargs.get("extra_body") or {}
        # Both shapes carried, per the openai_compat two-shape pattern
        assert "reasoning_effort" in extra
        assert "reasoning" in extra and "effort" in extra["reasoning"]
        # Default low; agreed with both providers
        assert extra["reasoning_effort"] == "low"

    def test_empty_stream_returns_empty_string(self) -> None:
        with patch.object(classifier_module, "_llm_call",
                          return_value=iter([])), \
             patch.object(classifier_module, "_get_openai_client",
                          return_value=MagicMock()):
            assert raw_completion(prompt="x") == ""

    def test_chunks_with_none_content_are_skipped(self) -> None:
        chunks = _stream_chunks(["", "ok"])
        # Inject a chunk with content=None mid-stream
        chunks.insert(1, SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None))],
        ))
        with patch.object(classifier_module, "_llm_call",
                          return_value=iter(chunks)), \
             patch.object(classifier_module, "_get_openai_client",
                          return_value=MagicMock()):
            assert raw_completion(prompt="x") == "ok"

    def test_non_iterable_response_falls_back_to_message_content(self) -> None:
        """Older mocks may return a Chat completion object (not an
        iterator). raw_completion's TypeError catch keeps them working."""
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="bar"))],
        )
        with patch.object(classifier_module, "_llm_call",
                          return_value=completion), \
             patch.object(classifier_module, "_get_openai_client",
                          return_value=MagicMock()):
            assert raw_completion(prompt="x") == "bar"

    def test_env_override_for_reasoning_effort(self, monkeypatch) -> None:
        monkeypatch.setenv("BITGN_CLASSIFIER_REASONING_EFFORT", "high")
        with patch.object(classifier_module, "_llm_call",
                          return_value=iter([])) as mock_llm, \
             patch.object(classifier_module, "_get_openai_client",
                          return_value=MagicMock()):
            raw_completion(prompt="x")
        extra = mock_llm.call_args.kwargs["extra_body"]
        assert extra["reasoning_effort"] == "high"
        assert extra["reasoning"]["effort"] == "high"
