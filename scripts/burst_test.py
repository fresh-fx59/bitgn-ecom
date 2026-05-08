"""Run the cliproxyapi rate-limit burst ladder.

Usage: python scripts/burst_test.py --output artifacts/burst/<ts>.json

Phase 1 cooldown: 60 s of idle before the first rung. Each rung runs
for 15 s steady state; errors accumulate in that window. A rung is a
"break" when rate_limit_errors >= 3 during its window. We stop at the
first break OR when the ceiling (96) clears cleanly.

After the primary pass, a secondary ~500-token sanity burst runs at
the cleared level (or the level just below the break) to check for
TPM-vs-RPM confusion.

PLAN DEVIATION: the plan pseudocode referenced ``load_config`` and
``OpenAICompatBackend``. The real names are ``load_from_env`` and
``OpenAIChatBackend.from_config``. The plan also passed raw dicts where
the backend expects ``Message`` dataclasses; we wrap them here.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import openai
from pydantic import BaseModel

from bitgn_contest_agent.backend.base import Message, TransientBackendError
from bitgn_contest_agent.backend.openai_compat import OpenAIChatBackend
from bitgn_contest_agent.bench.burst import (
    LADDER,
    InsufficientHeadroomError,
    pick_operating_point,
)
from bitgn_contest_agent.config import load_from_env


COOLDOWN_SEC = 60
WINDOW_SEC = 15
BREAK_THRESHOLD = 3

TRIVIAL_PROMPT = [
    Message(
        role="user",
        content='Reply with exactly this JSON object and nothing else: {"ok": true}',
    )
]
REALISTIC_PROMPT = [
    Message(
        role="user",
        content=(
            "Reply with exactly this JSON object and nothing else after "
            "considering the following: explain in 150 words the tradeoffs "
            "of eventual consistency versus strong consistency in "
            'distributed key-value stores. {"ok": true}'
        ),
    )
]


class _TrivialSchema(BaseModel):
    ok: bool


def _is_rate_limit(exc: Exception) -> bool:
    """True if the underlying cause is an openai.RateLimitError.

    TransientBackendError is the umbrella for rate limits, 5xx, and
    network timeouts; we look through ``__cause__`` so 5xx/network
    flakes don't inflate the rate-limit count.
    """
    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, openai.RateLimitError)


def _run_burst(backend, prompt, N: int, window_sec: int) -> dict:
    """Run N concurrent calls repeatedly for window_sec seconds, count errors."""
    start = time.monotonic()
    deadline = start + window_sec
    errors = 0
    completions = 0
    lock = threading.Lock()

    def one_call():
        nonlocal errors, completions
        try:
            backend.next_step(
                messages=prompt,
                response_schema=_TrivialSchema,
                timeout_sec=30.0,
            )
            with lock:
                completions += 1
        except TransientBackendError as exc:
            if _is_rate_limit(exc):
                with lock:
                    errors += 1
            # non-rate-limit transients (5xx, timeouts) are silently tolerated —
            # they are noise, not the signal the burst is measuring.
        except Exception:
            # Model returned malformed JSON, etc. — count as completion attempt
            # that didn't produce a rate-limit signal.
            pass

    with ThreadPoolExecutor(max_workers=N) as ex:
        while time.monotonic() < deadline:
            futs = [ex.submit(one_call) for _ in range(N)]
            for _ in as_completed(futs):
                pass
    return {
        "N": N,
        "completions": completions,
        "rate_limit_errors": errors,
        "window_sec": window_sec,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    args = p.parse_args()

    cfg = load_from_env()
    backend = OpenAIChatBackend.from_config(
        base_url=cfg.cliproxy_base_url,
        api_key=cfg.cliproxy_api_key,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )

    print(f"cooldown {COOLDOWN_SEC}s...")
    time.sleep(COOLDOWN_SEC)

    rungs: list[dict] = []
    first_break: Optional[int] = None
    last_cleared: Optional[int] = None
    for N in LADDER:
        print(f"burst N={N}...")
        r = _run_burst(backend, TRIVIAL_PROMPT, N, WINDOW_SEC)
        rungs.append(r)
        if r["rate_limit_errors"] >= BREAK_THRESHOLD:
            first_break = N
            break
        last_cleared = N

    # Secondary ~500-token sanity burst
    secondary_level = (first_break - 4) if first_break else last_cleared
    secondary: Optional[dict] = None
    if secondary_level and secondary_level >= 4:
        print(f"secondary realistic burst at N={secondary_level}...")
        secondary = _run_burst(backend, REALISTIC_PROMPT, secondary_level, WINDOW_SEC)

    try:
        chosen: Optional[int] = pick_operating_point(
            first_break_level=first_break,
            errors_at_break=(
                rungs[-1]["rate_limit_errors"] if first_break else 0
            ),
        )
    except InsufficientHeadroomError as e:
        print(f"FAIL: {e}")
        chosen = None

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ladder": LADDER,
        "cooldown_sec": COOLDOWN_SEC,
        "window_sec": WINDOW_SEC,
        "break_threshold": BREAK_THRESHOLD,
        "rungs": rungs,
        "first_break_level": first_break,
        "secondary": secondary,
        "chosen_max_inflight_llm": chosen,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, indent=2))
    print(f"wrote {args.output}")
    return 0 if chosen is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
