"""One-off PROD run-flow smoke test for the scraper.

Uses bitgn_contest_agent.harness.BitgnHarness because StartRun on the
live server requires `api_key` in the JSON body and the locally
installed bitgn wheel ships a stale protobuf descriptor without that
field — BitgnHarness already implements the raw-JSON-POST workaround.

Starts a single benchmark run via StartRun (so it shows up under "PROD"
in the dashboard, not "sandbox"/playground), picks 5 trials by task_id,
walks each workspace, submits one probe answer with OUTCOME_OK, and
ends each trial. Does NOT call SubmitRun — the run stays in-progress
on the dashboard so the scraper's deliberately-wrong probe answers do
NOT post a benchmark score to the leaderboard.

Output: artifacts/harness_db/scrape_runs/<ts>/run_smoke.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
sys.path = [p for p in sys.path if Path(p).resolve() != _SCRIPTS]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(prog="scrape_prod_run_smoke")
    parser.add_argument("--benchmark-id", default="bitgn/pac1-prod")
    parser.add_argument("--task-ids", default="t001,t010,t012,t017,t024",
                        help="comma-separated 5 task ids to probe")
    parser.add_argument("--name", default="",
                        help="run name; default: scraper-smoke-<ts>")
    parser.add_argument("--out-root", type=Path,
                        default=Path("artifacts/harness_db/scrape_runs"))
    args = parser.parse_args()

    from bitgn_contest_agent.harness import BitgnHarness
    from bitgn_scraper.workspace_walk import walk_workspace
    from bitgn.harness_pb2 import GetRunRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        raise SystemExit("BITGN_API_KEY is not set; source .env first")
    base_url = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")

    started_at = datetime.now(tz=timezone.utc)
    ts = started_at.strftime("%Y%m%d_%H%M%S")
    run_name = args.name or f"scraper-smoke-{ts}"
    wanted = [t.strip() for t in args.task_ids.split(",") if t.strip()]

    out_dir = args.out_root / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "run_smoke.json"

    harness = BitgnHarness.from_env(
        benchmark=args.benchmark_id,
        bitgn_base_url=base_url,
        bitgn_api_key=api_key,
    )

    print(f"[run-smoke] StartRun benchmark={args.benchmark_id} name={run_name!r}",
          flush=True)
    run_id, trial_ids = harness.start_run(name=run_name)
    print(f"[run-smoke] run_id={run_id} trials_allocated={len(trial_ids)}",
          flush=True)

    print("[run-smoke] GetRun → mapping trial_id → task_id", flush=True)
    run_view = harness._harness.get_run(GetRunRequest(run_id=run_id))
    trial_by_task = {t.task_id: t.trial_id for t in run_view.trials}
    missing = [t for t in wanted if t not in trial_by_task]
    if missing:
        print(f"[run-smoke] ERROR: requested tasks not in run trials: {missing}",
              flush=True)
        return 2

    findings: list[dict] = []
    for i, task_id in enumerate(wanted, 1):
        trial_id = trial_by_task[task_id]
        print(f"[run-smoke] ({i}/{len(wanted)}) StartTrial task={task_id} "
              f"trial={trial_id[:12]}…", flush=True)
        started = harness.start_trial(trial_id)
        files = walk_workspace(started.runtime_client)
        bytes_total = sum(f.byte_size for f in files)
        try:
            started.runtime_client.answer(
                AnswerRequest(message="alpha", outcome=Outcome.OUTCOME_OK)
            )
            answer_outcome = "accepted"
        except Exception as exc:  # noqa: BLE001
            answer_outcome = f"raised:{type(exc).__name__}:{exc!s}"
        score, score_detail = harness.end_task(started)
        finding = {
            "task_id": task_id,
            "trial_id": trial_id,
            "instruction_first_120": started.instruction[:120],
            "workspace_byte_total": bytes_total,
            "workspace_file_count": len(files),
            "answer_outcome": answer_outcome,
            "trial_score": score,
            "score_detail": list(score_detail),
        }
        findings.append(finding)
        print(f"  → bytes={bytes_total} score={score} "
              f"detail={list(score_detail)[:1]}", flush=True)

    # NOTE: deliberately not calling SubmitRun. The probes submit wrong
    # answers (OUTCOME_OK with "alpha") so submitting would post bogus
    # scores to the leaderboard. In-progress runs are still visible on
    # the dashboard.

    payload = {
        "started_at": started_at.isoformat(),
        "benchmark_id": args.benchmark_id,
        "run_id": run_id,
        "run_name": run_name,
        "trials_in_run": len(trial_ids),
        "wanted_task_ids": wanted,
        "findings": findings,
        "submit_run_called": False,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"[run-smoke] wrote {out_path}", flush=True)
    print(f"[run-smoke] dashboard should show run_id={run_id} "
          f"name={run_name!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
