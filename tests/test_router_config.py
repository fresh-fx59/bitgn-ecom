"""Tests for router tier-2 classifier configuration."""
from __future__ import annotations

import pytest

from bitgn_contest_agent import router_config


def test_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_CLASSIFIER_MODEL", raising=False)
    assert router_config.classifier_model() == router_config.DEFAULT_CLASSIFIER_MODEL


def test_default_model_is_a_cliproxy_mini() -> None:
    # Sanity check — the default must look like a small model id so we
    # don't accidentally ship a full-model default that would blow up
    # the per-task classifier cost.
    assert "mini" in router_config.DEFAULT_CLASSIFIER_MODEL or "haiku" in router_config.DEFAULT_CLASSIFIER_MODEL


def test_override_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_CLASSIFIER_MODEL", "test-model")
    assert router_config.classifier_model() == "test-model"


def test_confidence_threshold_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_CLASSIFIER_CONFIDENCE_THRESHOLD", raising=False)
    assert router_config.confidence_threshold() == 0.6


def test_confidence_threshold_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_CLASSIFIER_CONFIDENCE_THRESHOLD", "0.85")
    assert router_config.confidence_threshold() == 0.85


def test_confidence_threshold_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_CLASSIFIER_CONFIDENCE_THRESHOLD", "not-a-number")
    assert router_config.confidence_threshold() == 0.6


def test_router_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_ROUTER_ENABLED", raising=False)
    assert router_config.router_enabled() is True


def test_router_disabled_by_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_ROUTER_ENABLED", "0")
    assert router_config.router_enabled() is False


def test_router_disabled_by_false_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_ROUTER_ENABLED", "false")
    assert router_config.router_enabled() is False
