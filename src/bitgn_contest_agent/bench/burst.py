"""Synthetic rate-limit burst helpers.

pick_operating_point encodes Plan B's parallelism-selection rule.
Below 8 concurrent calls, we cannot sustain the required operating
region — we raise InsufficientHeadroomError so the script fails loudly
instead of silently picking an unworkable value.
"""
from __future__ import annotations

import math
from typing import Optional

LADDER: list[int] = [4, 8, 16, 32, 48, 64, 96]
DEFAULT_WHEN_CLEARED: int = 48
HEADROOM_MULTIPLIER: float = 0.6
MIN_USABLE_BREAK_LEVEL: int = 8


class InsufficientHeadroomError(RuntimeError):
    """Raised when the first burst break is below MIN_USABLE_BREAK_LEVEL."""


def pick_operating_point(*, first_break_level: Optional[int],
                          errors_at_break: int) -> int:
    """Return the chosen max_inflight_llm.

    first_break_level: lowest ladder rung where rate_limit_errors crossed
        the stop threshold, or None if we cleared every rung.
    errors_at_break: count of rate-limit errors at that rung (stored for
        the operating-point record but not used in the formula).
    """
    if first_break_level is None:
        return DEFAULT_WHEN_CLEARED
    if first_break_level < MIN_USABLE_BREAK_LEVEL:
        raise InsufficientHeadroomError(
            f"burst broke at N={first_break_level} < {MIN_USABLE_BREAK_LEVEL}"
        )
    return math.floor(HEADROOM_MULTIPLIER * first_break_level)
