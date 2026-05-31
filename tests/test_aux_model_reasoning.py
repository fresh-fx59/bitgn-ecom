"""Aux-LLM model selection + reasoning-param suppression.

Root cause (2026-05-31): on the linkapi provider the default aux model
``claude-haiku-4-5-20251001`` has a dead upstream channel — EVERY aux call
returns ``bad response status code 400`` — so an entire 100-task PROD run
ran with ``verification_coverage ... succeeded=0`` (classifier, normaliser,
ref_judge, judge_enforcer all blacked out). The cheap chat models that DO
work on linkapi (gpt-4.1-mini / gpt-4o-mini) reject the ``reasoning`` /
``reasoning_effort`` args with ``Unrecognized request arguments``.

Fix: (a) provider-aware default aux model so linkapi picks a working mini;
(b) suppress reasoning params for non-reasoning chat models.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import bitgn_contest_agent.classifier as classifier_module
from bitgn_contest_agent.classifier import _model_supports_reasoning, raw_completion
from bitgn_contest_agent.router_config import classifier_model


class TestProviderAwareAuxModel:
    def test_linkapi_profile_defaults_to_working_mini(self, monkeypatch) -> None:
        monkeypatch.delenv("BITGN_CLASSIFIER_MODEL", raising=False)
        monkeypatch.setenv("BITGN_PROVIDER_PROFILE", "linkapi")
        # The dead dated-Haiku must NOT be selected on linkapi.
        assert classifier_model() != "claude-haiku-4-5-20251001"
        assert classifier_model() == "gpt-4.1-mini"

    def test_explicit_override_always_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("BITGN_PROVIDER_PROFILE", "linkapi")
        monkeypatch.setenv("BITGN_CLASSIFIER_MODEL", "custom-model-x")
        assert classifier_model() == "custom-model-x"

    def test_cliproxy_profile_keeps_dated_haiku(self, monkeypatch) -> None:
        monkeypatch.delenv("BITGN_CLASSIFIER_MODEL", raising=False)
        monkeypatch.setenv("BITGN_PROVIDER_PROFILE", "cliproxyapi")
        assert classifier_model() == "claude-haiku-4-5-20251001"

    def test_unknown_profile_keeps_legacy_default(self, monkeypatch) -> None:
        monkeypatch.delenv("BITGN_CLASSIFIER_MODEL", raising=False)
        monkeypatch.delenv("BITGN_PROVIDER_PROFILE", raising=False)
        assert classifier_model() == "claude-haiku-4-5-20251001"


class TestModelSupportsReasoning:
    def test_plain_chat_models_excluded(self) -> None:
        for m in ("gpt-4.1-mini", "gpt-4o-mini", "gpt-4o", "gpt-4.1",
                  "gpt-4-turbo", "gpt-3.5-turbo"):
            assert _model_supports_reasoning(m) is False, m

    def test_reasoning_models_included(self) -> None:
        for m in ("gpt-5.4", "gpt-5.3-codex", "claude-haiku-4-5-20251001",
                  "o3-mini", "anthropic/claude-haiku-4.5"):
            assert _model_supports_reasoning(m) is True, m


def _patched_call():
    """Patch the transport + client so raw_completion records the kwargs."""
    mock_llm = patch.object(classifier_module, "_llm_call", return_value=iter([]))
    mock_client = patch.object(classifier_module, "_get_openai_client",
                               return_value=MagicMock())
    return mock_llm, mock_client


class TestReasoningParamSuppression:
    def test_non_reasoning_model_omits_reasoning_args(self, monkeypatch) -> None:
        monkeypatch.setenv("BITGN_CLASSIFIER_MODEL", "gpt-4.1-mini")
        m_llm, m_client = _patched_call()
        with m_llm as mock_llm, m_client:
            raw_completion(prompt="x")
        extra = mock_llm.call_args.kwargs.get("extra_body") or {}
        assert "reasoning" not in extra
        assert "reasoning_effort" not in extra

    def test_reasoning_model_keeps_reasoning_args(self, monkeypatch) -> None:
        monkeypatch.setenv("BITGN_CLASSIFIER_MODEL", "gpt-5.4")
        m_llm, m_client = _patched_call()
        with m_llm as mock_llm, m_client:
            raw_completion(prompt="x")
        extra = mock_llm.call_args.kwargs.get("extra_body") or {}
        assert extra.get("reasoning_effort") == "low"
        assert extra.get("reasoning", {}).get("effort") == "low"

    def test_effort_none_omits_args_even_for_reasoning_model(self, monkeypatch) -> None:
        monkeypatch.setenv("BITGN_CLASSIFIER_MODEL", "gpt-5.4")
        monkeypatch.setenv("BITGN_CLASSIFIER_REASONING_EFFORT", "none")
        m_llm, m_client = _patched_call()
        with m_llm as mock_llm, m_client:
            raw_completion(prompt="x")
        extra = mock_llm.call_args.kwargs.get("extra_body") or {}
        assert "reasoning" not in extra
        assert "reasoning_effort" not in extra
