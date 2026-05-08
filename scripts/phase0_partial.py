"""Run only the Phase 0 sub-spikes whose answers we don't already know.

User-confirmed (2026-04-26) without measurement:
  - rotation:         rotates per call
  - url_lifetime:     reachable ~forever after EndTrial
  - auto_termination: does not happen
  - rate_limit:       no throttling observed

Measured here:
  - state_isolation
  - answer_replay
  - size_sanity

Output: artifacts/harness_db/scrape_runs/<ts>/lifecycle_spike.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# When invoked via `uv run python scripts/...`, Python inserts `scripts/`
# at sys.path[0], which shadows the `bitgn_scraper` package in src/. Strip
# scripts/ entries and prepend src/ — same pattern as scripts/bitgn_scraper.py.
_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
sys.path = [p for p in sys.path if Path(p).resolve() != _SCRIPTS]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(prog="phase0_partial")
    parser.add_argument("--task-id", default="t001",
                        help="task to use for state_isolation + answer_replay")
    parser.add_argument("--size-sample", default="t001,t010,t020,t030,t050",
                        help="comma-separated task ids for size_sanity")
    parser.add_argument("--out-root", type=Path,
                        default=Path("artifacts/harness_db/scrape_runs"))
    args = parser.parse_args()

    from bitgn_scraper.clients import build_harness_client, build_pcm_client
    from bitgn_scraper.phase0 import (
        _spike_size_sanity,
        _spike_state_isolation,
    )
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome
    from connectrpc.errors import ConnectError
    from dataclasses import asdict

    client = build_harness_client()
    started_at = datetime.now(tz=timezone.utc)
    print(f"[phase0_partial] starting at {started_at.isoformat()}", flush=True)

    print(f"[phase0_partial] (1/3) state_isolation on {args.task_id}", flush=True)
    state_isolation = _spike_state_isolation(client, args.task_id)
    print(f"  → second_trial_saw_write={state_isolation.second_trial_saw_write}", flush=True)

    # Inline answer-replay: the canonical _spike_answer_replay assumes both
    # Answer calls succeed, but PROD raises ConnectError on the second one
    # ("Answer was already provided"). We capture that as a finding instead
    # of crashing.
    print(f"[phase0_partial] (2/3) answer_replay on {args.task_id}", flush=True)
    started = client.start_playground(
        StartPlaygroundRequest(benchmark_id="bitgn/pac1-prod", task_id=args.task_id)
    )
    pcm = build_pcm_client(started.harness_url)
    pcm.answer(AnswerRequest(message="alpha", outcome=Outcome.OUTCOME_OK))
    second_answer_outcome = "accepted"
    second_answer_error = ""
    try:
        pcm.answer(AnswerRequest(message="beta", outcome=Outcome.OUTCOME_OK))
    except ConnectError as exc:
        second_answer_outcome = "rejected"
        second_answer_error = str(exc)
    ended = client.end_trial(EndTrialRequest(trial_id=started.trial_id))
    detail_text = " ".join(ended.score_detail) if ended.score_detail else ""
    if second_answer_outcome == "rejected":
        graded = "first"  # second never reached the grader
    elif "alpha" in detail_text and "beta" not in detail_text:
        graded = "first"
    elif "beta" in detail_text and "alpha" not in detail_text:
        graded = "second"
    else:
        graded = "unknown"
    answer_replay = {
        "first_answer": "alpha",
        "second_answer": "beta",
        "second_answer_outcome": second_answer_outcome,
        "second_answer_error": second_answer_error,
        "trial_score": ended.score,
        "score_detail": list(ended.score_detail),
        "graded_against": graded,
    }
    print(f"  → second_answer={second_answer_outcome}; graded_against={graded!r}", flush=True)

    sample_ids = args.size_sample.split(",")
    print(f"[phase0_partial] (3/3) size_sanity on {sample_ids}", flush=True)
    size_sanity = _spike_size_sanity(client, sample_ids)
    print(f"  → max_byte_total={size_sanity.max_byte_total}", flush=True)

    payload = {
        "started_at": started_at.isoformat(),
        "source_note": (
            "Partial Phase 0 run — 4 sub-spikes resolved by user-confirmed facts "
            "(2026-04-26), 3 sub-spikes measured below."
        ),
        "user_confirmed": {
            "rotation": {
                "behavior": "instruction text rotates per StartPlayground call for the same task_id",
                "implication": "local clone must serve N variants per task",
            },
            "url_lifetime": {
                "behavior": "harness_url stays reachable effectively forever after EndTrial",
                "implication": "no URL-expiration handling needed in clients",
            },
            "auto_termination": {
                "behavior": "unended trials are NOT auto-terminated by the harness",
                "implication": "no stale-trial GC behavior to model",
            },
            "rate_limit": {
                "behavior": "no throttling observed on StartPlayground burst calls",
                "implication": "no backoff strategy required",
            },
        },
        "measured": {
            "state_isolation": asdict(state_isolation),
            "answer_replay": answer_replay,
            "size_sanity": asdict(size_sanity),
        },
    }

    out_dir = args.out_root / started_at.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "lifecycle_spike.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"[phase0_partial] wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
