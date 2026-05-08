"""Tests for AGENT_MAX_BACKOFF_SEC extension of rate_limit_backoff_ms."""
from __future__ import annotations

import os

import pytest

from bitgn_contest_agent.config import _build_backoff_schedule


def test_build_backoff_schedule_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_BACKOFF_SEC", raising=False)
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000)


def test_build_backoff_schedule_disabled_by_zero(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "0")
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000)


def test_build_backoff_schedule_appends_tail_when_positive(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "300")
    # default head + 30s bridge + 300s tail
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000, 30_000, 300_000)


def test_build_backoff_schedule_ignores_negative(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "-5")
    assert _build_backoff_schedule() == (500, 1500, 4000, 10000)


def test_build_backoff_schedule_rejects_nonnumeric(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BACKOFF_SEC", "foo")
    with pytest.raises(ValueError):
        _build_backoff_schedule()
