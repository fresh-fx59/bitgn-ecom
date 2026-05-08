"""Smoke test — adapter constructs + exposes dispatch + prepass API."""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult


def test_adapter_constructs_from_runtime_client() -> None:
    runtime = MagicMock()
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)
    assert callable(adapter.dispatch)
    assert callable(adapter.run_prepass)


def test_tool_result_carries_ok_bytes_refs() -> None:
    r = ToolResult(
        ok=True,
        content="hello",
        refs=("AGENTS.md",),
        error=None,
        error_code=None,
        wall_ms=12,
    )
    assert r.ok
    assert r.bytes == len(b"hello")
    assert r.truncated is False
    assert r.refs == ("AGENTS.md",)
