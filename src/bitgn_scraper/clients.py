# src/bitgn_scraper/clients.py
"""Authenticated Connect-RPC client factories.

Reads BITGN_API_KEY (required) and BITGN_BASE_URL (optional, default
https://api.bitgn.com) from the env. Mirrors the auth pattern from
scripts/verify_prod_grader.py so the scraper and the existing probe
script both go through the same interceptor.
"""
from __future__ import annotations

import os
from typing import Any

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from connectrpc.interceptor import MetadataInterceptorSync


class _AuthInterceptor(MetadataInterceptorSync):
    def __init__(self, key: str) -> None:
        self._key = key

    def on_start_sync(self, ctx: Any) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._key}"
        return None


def _api_key() -> str:
    key = os.environ.get("BITGN_API_KEY")
    if not key:
        raise RuntimeError(
            "BITGN_API_KEY is not set. Source .env first: "
            "`set -a && source .worktrees/plan-b/.env && set +a`"
        )
    return key


def build_harness_client() -> HarnessServiceClientSync:
    """Build an authenticated HarnessService client pointed at PROD."""
    key = _api_key()
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com").rstrip("/")
    client = HarnessServiceClientSync(base, interceptors=(_AuthInterceptor(key),))
    # Pin the base URL on the client object so tests + diagnostics can
    # introspect what we connected to without re-reading the env.
    client._scraper_base_url = base  # type: ignore[attr-defined]
    return client


def build_pcm_client(harness_url: str) -> PcmRuntimeClientSync:
    """Build an authenticated PCM client for a specific trial sandbox."""
    if not harness_url:
        raise ValueError("harness_url is required (got empty string)")
    key = _api_key()
    base = harness_url.rstrip("/")
    return PcmRuntimeClientSync(base, interceptors=(_AuthInterceptor(key),))
