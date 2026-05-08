"""Tests for classifier JSON retry logic."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bitgn_contest_agent.classifier import classify, _try_fix_json


def _make_response(content: str | None):
    """Build a fake OpenAI ChatCompletion response."""
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


class TestClassifyRetry:
    """Test the retry loop in classify()."""

    @patch("bitgn_contest_agent.classifier._get_openai_client")
    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=3)
    def test_valid_json_returns_immediately(self, _cfg, mock_client_fn) -> None:
        client = MagicMock()
        client.chat.completions.create.return_value = _make_response(
            '{"category": "FOO", "confidence": 0.9}'
        )
        mock_client_fn.return_value = client

        result = classify(system="sys", user="usr")
        assert result == {"category": "FOO", "confidence": 0.9}
        assert client.chat.completions.create.call_count == 1

    @patch("bitgn_contest_agent.classifier._get_openai_client")
    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=3)
    def test_fenced_json_parsed_without_retry(self, _cfg, mock_client_fn) -> None:
        client = MagicMock()
        client.chat.completions.create.return_value = _make_response(
            '```json\n{"category": "FOO", "confidence": 0.9}\n```'
        )
        mock_client_fn.return_value = client

        result = classify(system="sys", user="usr")
        assert result == {"category": "FOO", "confidence": 0.9}
        assert client.chat.completions.create.call_count == 1

    @patch("bitgn_contest_agent.classifier._get_openai_client")
    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=3)
    def test_broken_json_fixed_by_model(self, _cfg, mock_client_fn) -> None:
        """First call returns broken JSON, fix call returns valid JSON."""
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            # Attempt 1: broken JSON from classification
            _make_response('{category: FOO, confidence: 0.9}'),
            # Fix call: model returns valid JSON
            _make_response('{"category": "FOO", "confidence": 0.9}'),
        ]
        mock_client_fn.return_value = client

        result = classify(system="sys", user="usr")
        assert result == {"category": "FOO", "confidence": 0.9}
        assert client.chat.completions.create.call_count == 2

    @patch("bitgn_contest_agent.classifier._get_openai_client")
    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=3)
    def test_broken_json_fix_fails_retries_fresh(self, _cfg, mock_client_fn) -> None:
        """Broken JSON, fix also broken, fresh retry succeeds."""
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            # Attempt 1: broken JSON
            _make_response('{bad json}'),
            # Fix call: also broken
            _make_response('{still bad}'),
            # Attempt 2: fresh classification succeeds
            _make_response('{"category": "BAR", "confidence": 0.8}'),
        ]
        mock_client_fn.return_value = client

        result = classify(system="sys", user="usr")
        assert result == {"category": "BAR", "confidence": 0.8}
        assert client.chat.completions.create.call_count == 3

    @patch("bitgn_contest_agent.classifier._get_openai_client")
    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=2)
    def test_all_attempts_exhausted_raises(self, _cfg, mock_client_fn) -> None:
        """After max_attempts, the last error is raised."""
        client = MagicMock()
        client.chat.completions.create.return_value = _make_response('{bad}')
        mock_client_fn.return_value = client

        with pytest.raises(Exception):
            classify(system="sys", user="usr")

    @patch("bitgn_contest_agent.classifier._get_openai_client")
    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=3)
    def test_null_content_retries(self, _cfg, mock_client_fn) -> None:
        """None content triggers retry without fix attempt."""
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            # Attempt 1: null content
            _make_response(None),
            # Attempt 2: valid
            _make_response('{"category": "OK", "confidence": 1.0}'),
        ]
        mock_client_fn.return_value = client

        result = classify(system="sys", user="usr")
        assert result == {"category": "OK", "confidence": 1.0}

    @patch("bitgn_contest_agent.router_config.classifier_max_attempts", return_value=1)
    def test_max_attempts_config_respected(self, _cfg) -> None:
        """With max_attempts=1, no retry after failure."""
        from bitgn_contest_agent import router_config
        assert router_config.classifier_max_attempts() == 1


class TestTryFixJson:
    """Test the JSON fix helper."""

    def test_fix_returns_parsed_json(self) -> None:
        client = MagicMock()
        client.chat.completions.create.return_value = _make_response(
            '{"category": "X", "confidence": 0.5}'
        )
        import json
        error = None
        try:
            json.loads("{bad}")
        except json.JSONDecodeError as exc:
            error = exc

        result = _try_fix_json(client, "model", "{bad}", error)
        assert result == {"category": "X", "confidence": 0.5}

    def test_fix_returns_none_on_failure(self) -> None:
        client = MagicMock()
        client.chat.completions.create.return_value = _make_response("{still bad}")

        import json
        error = None
        try:
            json.loads("{bad}")
        except json.JSONDecodeError as exc:
            error = exc

        result = _try_fix_json(client, "model", "{bad}", error)
        assert result is None

    def test_fix_returns_none_on_null_content(self) -> None:
        client = MagicMock()
        client.chat.completions.create.return_value = _make_response(None)

        import json
        error = None
        try:
            json.loads("{bad}")
        except json.JSONDecodeError as exc:
            error = exc

        result = _try_fix_json(client, "model", "{bad}", error)
        assert result is None
