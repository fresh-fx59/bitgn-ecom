"""Verification spike — does lmstudio-python `.cancel()` actually stop
generation on LM Studio at localhost:1236?

Acceptance checks (printed at end):
  1. Cancel call returns without raising.
  2. Iteration loop exits promptly after cancel (wall-clock delta small).
  3. Follow-up request to the same model slot returns a short result
     within a few seconds — proves the slot was freed (stalled slot
     would block this next request).
  4. Total tokens observed on the cancelled stream is < what the model
     would produce if uncancelled (soft — depends on model speed).

Run:   .venv/bin/python scripts/spike_lmstudio_cancel.py
"""
from __future__ import annotations

import sys
import time

import lmstudio as lms


MODEL = "qwen3.5-35b-a3b"
HOST = "localhost:1236"
# Long prompt that triggers extended reasoning. Paired with max_tokens high
# enough that uncancelled generation would run many seconds.
LONG_PROMPT = (
    "Write a detailed 3000-word analysis of the factors influencing "
    "the evolution of distributed consensus protocols from Paxos through "
    "Raft to modern BFT variants. Include tradeoffs, examples, and "
    "historical context. Be thorough."
)
CANCEL_AFTER_SECONDS = 3.0
MAX_TOKENS = 8000


def main() -> int:
    client = lms.Client(HOST)
    model = client.llm.model(MODEL)
    print(f"[spike] connected to {HOST}, model={MODEL}")

    # ---------------- Phase 1: start + cancel ----------------
    print(f"[spike] phase 1 — starting generation, will cancel after "
          f"{CANCEL_AFTER_SECONDS}s")
    stream = model.respond_stream(
        LONG_PROMPT,
        config={"maxTokens": MAX_TOKENS, "temperature": 0.7},
    )

    tokens_before_cancel = 0
    cancel_sent_at: float | None = None
    iter_exited_at: float | None = None
    start = time.monotonic()
    cancel_exception: BaseException | None = None

    try:
        for fragment in stream:
            now = time.monotonic()
            tokens_before_cancel += 1
            if cancel_sent_at is None and (now - start) >= CANCEL_AFTER_SECONDS:
                print(f"[spike]   {tokens_before_cancel} fragments received "
                      f"before cancel, calling stream.cancel() at "
                      f"t={now - start:.2f}s")
                try:
                    stream.cancel()
                except BaseException as exc:
                    cancel_exception = exc
                    print(f"[spike]   cancel() RAISED: {exc!r}")
                cancel_sent_at = now
        iter_exited_at = time.monotonic()
    except BaseException as exc:
        iter_exited_at = time.monotonic()
        print(f"[spike]   iteration raised: {exc!r}")

    assert cancel_sent_at is not None, "never reached cancel trigger"
    assert iter_exited_at is not None
    cancel_to_exit = iter_exited_at - cancel_sent_at
    print(f"[spike]   fragments seen: {tokens_before_cancel}")
    print(f"[spike]   time from cancel → iteration exit: "
          f"{cancel_to_exit:.3f}s")

    # ---------------- Phase 2: slot free? ----------------
    print("[spike] phase 2 — issuing follow-up request; if the slot is "
          "still busy the ‘Taking no action’ warning is live and this "
          "will block")
    followup_start = time.monotonic()
    followup = model.respond(
        "Reply with exactly the word: READY",
        config={"maxTokens": 8, "temperature": 0.0},
    )
    followup_elapsed = time.monotonic() - followup_start
    followup_content = (followup.content or "").strip()
    print(f"[spike]   followup elapsed: {followup_elapsed:.2f}s, "
          f"content={followup_content!r}")

    # ---------------- Verdict ----------------
    print()
    print("=" * 60)
    print("VERDICT")
    print("=" * 60)
    checks = {
        "cancel() returned cleanly (no exception)": cancel_exception is None,
        "iteration exited within 5s of cancel": cancel_to_exit < 5.0,
        "followup returned within 10s": followup_elapsed < 10.0,
        "followup content non-empty": bool(followup_content),
    }
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    overall_pass = all(checks.values())
    print()
    print(f"overall: {'PASS' if overall_pass else 'FAIL'}")
    print("(Watch LM Studio console for the 'Taking no action' warning: "
          "absence + slot free = real cancel working.)")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
