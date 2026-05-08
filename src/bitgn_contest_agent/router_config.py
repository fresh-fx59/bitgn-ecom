"""Router tier-2 classifier configuration.

Resolved in M0 task 3 after probing the cliproxyapi model catalog on
2026-04-12. The env var `BITGN_CLASSIFIER_MODEL` overrides.

Available classifier-sized models in the local cliproxyapi catalog:

    gpt-5.4-mini            (OpenAI — content=null bug via proxy)
    gpt-5.1-codex-mini      (OpenAI — content=null bug via proxy)
    gpt-5-codex-mini        (OpenAI)
    claude-haiku-4-5        (Anthropic — DEFAULT, reliable via proxy)
    claude-3-5-haiku        (Anthropic)

`claude-haiku-4-5-20251001` is picked as the default because the
OpenAI mini models return content=null through the cliproxyapi proxy
(the proxy strips completion content from reasoning-capable minis).
Haiku is cheap, fast, and reliably returns JSON content.
"""
from __future__ import annotations

import os

DEFAULT_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Confidence threshold below which a classifier response is treated as
# UNKNOWN. Set to 0.6 in the spec §5.3.
DEFAULT_CONFIDENCE_THRESHOLD = 0.6


def classifier_model() -> str:
    return os.environ.get("BITGN_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)


def confidence_threshold() -> float:
    raw = os.environ.get("BITGN_CLASSIFIER_CONFIDENCE_THRESHOLD")
    if raw is None:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD


def router_enabled() -> bool:
    return os.environ.get("BITGN_ROUTER_ENABLED", "1") not in ("0", "false", "False")


# Maximum number of classify attempts before giving up. Each attempt
# includes one classify call + at most one "fix" call if JSON is broken.
DEFAULT_CLASSIFIER_MAX_ATTEMPTS = 3


def classifier_max_attempts() -> int:
    raw = os.environ.get("BITGN_CLASSIFIER_MAX_ATTEMPTS")
    if raw is None:
        return DEFAULT_CLASSIFIER_MAX_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_CLASSIFIER_MAX_ATTEMPTS
