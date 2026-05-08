"""Per-model adapter registry.

``get_adapter(model)`` returns the ``ModelAdapter`` for the given exact
model string, or raises ``ConfigError`` listing the registered keys. The
registry is only consulted from ``OpenAIToolCallingBackend.from_config``,
which itself is only reached when ``AGENT_TOOLCALLING=1``. The frontier
path (``OpenAIChatBackend``) never calls ``get_adapter``.
"""
from __future__ import annotations

from typing import Dict, Type

from bitgn_contest_agent.config import ConfigError

from .base import ModelAdapter, ModelProfile
from .glm_flash import GlmFlashAdapter
from .gpt_oss import GptOssAdapter
from .gpt_oss_remote import GptOssRemoteAdapter
from .lfm2 import Lfm2Adapter
from .qwen_a3b import QwenA3bAdapter
from .qwen_a3b_remote import QwenA3bRemoteAdapter


ADAPTERS: Dict[str, Type[ModelAdapter]] = {
    "openai/gpt-oss-20b": GptOssAdapter,
    # gpt-oss-120b is only served via the neuraldeep gateway today;
    # the remote adapter drops lmstudio_host (no local watchdog) and
    # caps llm_http_timeout at 65s to match the gateway's 60s cap.
    # A local 120b deployment would register a second entry.
    "gpt-oss-120b": GptOssRemoteAdapter,
    "glm-4.7-flash-mlx": GlmFlashAdapter,
    "liquid/lfm2-24b-a2b": Lfm2Adapter,
    "qwen3.5-35b-a3b": QwenA3bAdapter,
    # qwen3.6 is only served via the neuraldeep gateway today; the
    # remote adapter swaps the reasoning flag (extra_body.thinking)
    # and caps llm_http_timeout at 65s to match the gateway's 60s
    # internal cap. If qwen3.6 ever gets served locally via LM Studio,
    # add a second registry entry mapping to QwenA3bAdapter.
    "qwen3.6-35b-a3b": QwenA3bRemoteAdapter,
}


def get_adapter(model: str) -> ModelAdapter:
    """Return the adapter for ``model``, fail-fast on unknown."""
    cls = ADAPTERS.get(model)
    if cls is None:
        raise ConfigError(
            f"No adapter registered for AGENT_MODEL={model!r}. "
            f"Registered: {sorted(ADAPTERS)}. "
            f"Add one in src/bitgn_contest_agent/backend/adapters/."
        )
    return cls()


__all__ = [
    "ADAPTERS",
    "ModelAdapter",
    "ModelProfile",
    "get_adapter",
]
