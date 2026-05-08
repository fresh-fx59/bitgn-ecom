"""AgentConfig: all tunables and credentials in one dataclass.

Loaded once at startup from environment variables. Fail-fast validation
(§4 pattern P6) runs before the thread pool is created.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    # Credentials
    bitgn_api_key: str
    cliproxy_base_url: str
    cliproxy_api_key: str

    # Benchmark
    benchmark: str = "bitgn/pac1-dev"

    # Model
    model: str = "gpt-5.3-codex"
    reasoning_effort: str = "medium"

    # Timeouts / steps (§4.1 calibrated defaults)
    max_steps: int = 40
    task_timeout_sec: int = 900
    task_timeout_grace_sec: int = 20
    llm_http_timeout_sec: int = 30
    max_tool_result_bytes: int = 16384

    # Parallelism (§3.1) — tuned in Plan B T2.6 from burst artifact
    # artifacts/burst/20260411T061748Z.json (ladder cleared through
    # N=96, pick_operating_point → DEFAULT_WHEN_CLEARED=48, then
    # max_parallel_tasks = min(48, 8) = 8).
    max_parallel_tasks: int = 8
    max_inflight_llm: int = 48

    # Backend retry (§3.3)
    rate_limit_backoff_ms: Tuple[int, ...] = (500, 1500, 4000, 10000)

    # Tracing
    log_dir: str = "logs"

    @property
    def cancel_enabled(self) -> bool:
        return self.task_timeout_sec > 0


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"required environment variable {name} is missing or empty")
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


_DEFAULT_BACKOFF_MS: Tuple[int, ...] = (500, 1500, 4000, 10000)


def _build_backoff_schedule() -> Tuple[int, ...]:
    """Build the backend retry schedule, optionally with a long tail.

    The built-in schedule (16s total) is tuned for a healthy remote.
    Local inference (LM Studio on a single GPU) crashes or reloads for
    tens of seconds at a time, and PAC1 can return a brief 502 during
    a deploy — neither recovers inside 16s. Setting
    AGENT_MAX_BACKOFF_SEC=N appends a 30s bridge step and a final
    N-second step so a single backend call can survive an outage up
    to ~(16 + 30 + N) seconds before giving up. The trial-level
    TASK_TIMEOUT_SEC caps the overall trial budget, so there is no
    need to clamp AGENT_MAX_BACKOFF_SEC here.

    AGENT_MAX_BACKOFF_SEC=0 (or unset) keeps the short schedule —
    preserves the original behavior for frontier backends where a
    long tail would just waste time.
    """
    raw = os.environ.get("AGENT_MAX_BACKOFF_SEC")
    if raw is None:
        return _DEFAULT_BACKOFF_MS
    extra = int(raw)  # raises ValueError on non-numeric, which is desirable
    if extra <= 0:
        return _DEFAULT_BACKOFF_MS
    return _DEFAULT_BACKOFF_MS + (30_000, extra * 1000)


def load_from_env() -> AgentConfig:
    return AgentConfig(
        bitgn_api_key=_require("BITGN_API_KEY"),
        cliproxy_base_url=_require("CLIPROXY_BASE_URL"),
        cliproxy_api_key=_require("CLIPROXY_API_KEY"),
        benchmark=os.environ.get("BITGN_BENCHMARK", "bitgn/pac1-dev"),
        model=os.environ.get("AGENT_MODEL", "gpt-5.3-codex"),
        reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT", "medium"),
        max_steps=_int_env("MAX_STEPS", 40),
        task_timeout_sec=_int_env("TASK_TIMEOUT_SEC", 900),
        task_timeout_grace_sec=_int_env("TASK_TIMEOUT_GRACE_SEC", 20),
        llm_http_timeout_sec=_int_env("LLM_HTTP_TIMEOUT_SEC", 30),
        max_tool_result_bytes=_int_env("MAX_TOOL_RESULT_BYTES", 16384),
        max_parallel_tasks=_int_env("MAX_PARALLEL_TASKS", 4),
        max_inflight_llm=_int_env("MAX_INFLIGHT_LLM", 6),
        rate_limit_backoff_ms=_build_backoff_schedule(),
        log_dir=os.environ.get("LOG_DIR", "logs"),
    )
