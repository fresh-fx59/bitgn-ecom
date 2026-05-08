"""Tests for the AgentConfig dataclass + env loader."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.config import AgentConfig, ConfigError, load_from_env


def test_load_from_env_reads_all_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "bg-key-123")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://127.0.0.1:8317/v1")
    monkeypatch.setenv("CLIPROXY_API_KEY", "cp-key-abc")
    monkeypatch.setenv("BITGN_BENCHMARK", "bitgn/pac1-dev")
    cfg = load_from_env()
    assert isinstance(cfg, AgentConfig)
    assert cfg.bitgn_api_key == "bg-key-123"
    assert cfg.cliproxy_base_url == "http://127.0.0.1:8317/v1"
    assert cfg.cliproxy_api_key == "cp-key-abc"
    assert cfg.benchmark == "bitgn/pac1-dev"
    # §4.1 calibrated defaults
    assert cfg.model == "gpt-5.3-codex"
    assert cfg.reasoning_effort == "medium"
    assert cfg.max_steps == 40
    assert cfg.task_timeout_sec == 900
    assert cfg.task_timeout_grace_sec == 20
    assert cfg.llm_http_timeout_sec == 30
    assert cfg.max_tool_result_bytes == 16384
    assert cfg.max_parallel_tasks == 4
    assert cfg.max_inflight_llm == 6
    assert cfg.rate_limit_backoff_ms == (500, 1500, 4000, 10000)


def test_load_from_env_fails_fast_without_bitgn_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITGN_API_KEY", raising=False)
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://127.0.0.1:8317/v1")
    monkeypatch.setenv("CLIPROXY_API_KEY", "cp")
    with pytest.raises(ConfigError, match="BITGN_API_KEY"):
        load_from_env()


def test_task_timeout_zero_disables_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGN_API_KEY", "x")
    monkeypatch.setenv("CLIPROXY_BASE_URL", "http://localhost")
    monkeypatch.setenv("CLIPROXY_API_KEY", "y")
    monkeypatch.setenv("TASK_TIMEOUT_SEC", "0")
    cfg = load_from_env()
    assert cfg.task_timeout_sec == 0
    assert cfg.cancel_enabled is False


def test_load_from_env_task_timeout_default_matches_dataclass() -> None:
    """Regression: commit 87e9a4d bumped the dataclass default 300->600
    but missed this env loader default. The resulting effective timeout
    was 300s, not 600s as intended. Both defaults must stay in sync.
    """
    import os

    required = {
        "BITGN_API_KEY": "x",
        "CLIPROXY_BASE_URL": "http://localhost",
        "CLIPROXY_API_KEY": "x",
    }
    saved = {k: os.environ.get(k) for k in list(required) + ["TASK_TIMEOUT_SEC"]}
    try:
        for k, v in required.items():
            os.environ[k] = v
        os.environ.pop("TASK_TIMEOUT_SEC", None)
        cfg = load_from_env()
        dataclass_default = AgentConfig.__dataclass_fields__[
            "task_timeout_sec"
        ].default
        assert cfg.task_timeout_sec == dataclass_default, (
            f"env loader default {cfg.task_timeout_sec} != "
            f"dataclass default {dataclass_default}"
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
