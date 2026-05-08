# tests/scraper/test_clients.py
"""SDK client factory tests.

Network is never touched: we just verify the auth interceptor is wired
and that BITGN_BASE_URL is honored.
"""
from __future__ import annotations

import pytest

from bitgn_scraper.clients import build_harness_client, build_pcm_client


def test_build_harness_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BITGN_API_KEY"):
        build_harness_client()


def test_build_harness_client_uses_default_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.delenv("BITGN_BASE_URL", raising=False)
    client = build_harness_client()
    # The Connect-RPC sync client doesn't expose .base directly; check
    # the captured argument via the factory's debug attribute.
    assert client._scraper_base_url == "https://api.bitgn.com"


def test_build_harness_client_honors_base_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "fake")
    monkeypatch.setenv("BITGN_BASE_URL", "https://staging.bitgn.com/")
    client = build_harness_client()
    assert client._scraper_base_url == "https://staging.bitgn.com"


def test_build_pcm_client_requires_harness_url() -> None:
    with pytest.raises(ValueError, match="harness_url"):
        build_pcm_client("")
