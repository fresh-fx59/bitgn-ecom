"""Full PROD run-flow scrape (Plan 1B T10) — parallel.

Walks every trial in a single PROD run via the leaderboard flow:
  StartRun → for each trial (in a ThreadPoolExecutor): StartTrial →
  walk_workspace → answer probe → EndTrial (capturing harness_url +
  score). Does NOT call SubmitRun.

After the run loop, fetches the public per-trial transcript at
``<harness_url>?format=json`` via harness_url_scrape (also parallel).

Outputs (under artifacts/harness_db/scrape_runs/<ts>/):
  - run_summary.json          — top-level index of every trial
  - trials/<task_id>.json     — per-trial dump (instruction, walk metadata,
                                score detail, parsed transcript)

The probe deliberately submits ``alpha``/OUTCOME_OK on every trial
(grader returns 0). SubmitRun is intentionally skipped to keep these
scores off the leaderboard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
sys.path = [p for p in sys.path if Path(p).resolve() != _SCRIPTS]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(prog="scrape_prod_full")
    parser.add_argument("--benchmark-id", default="bitgn/pac1-prod")
    parser.add_argument("--name", default="",
                        help="run name; default: scraper-full-<ts>")
    parser.add_argument("--out-root", type=Path,
                        default=Path("artifacts/harness_db/scrape_runs"))
    parser.add_argument("--limit", type=int, default=0,
                        help="walk only first N trials (debug); 0 = all")
    parser.add_argument("--max-workers", type=int, default=40,
                        help="parallel workers for trial walks + transcript fetch")
    parser.add_argument("--dump-workspaces", action="store_true",
                        help="capture every file's content under "
                             "<out_dir>/workspaces/<task_id>/ for offline replay")
    args = parser.parse_args()

    from bitgn_contest_agent.harness import BitgnHarness
    from bitgn_scraper.harness_url_scrape import fetch_trial_data
    from bitgn_scraper.workspace_walk import walk_and_dump_workspace, walk_workspace
    from bitgn.harness_pb2 import GetRunRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        raise SystemExit("BITGN_API_KEY is not set; source .env first")
    base_url = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")

    started_at = datetime.now(tz=timezone.utc)
    ts = started_at.strftime("%Y%m%d_%H%M%S")
    run_name = args.name or f"scraper-full-{ts}"

    out_dir = args.out_root / ts
    trials_dir = out_dir / "trials"
    trials_dir.mkdir(parents=True, exist_ok=True)
    workspaces_dir = out_dir / "workspaces"
    if args.dump_workspaces:
        workspaces_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "run_summary.json"

    harness = BitgnHarness.from_env(
        benchmark=args.benchmark_id,
        bitgn_base_url=base_url,
        bitgn_api_key=api_key,
    )

    print(f"[full] StartRun benchmark={args.benchmark_id} name={run_name!r}",
          flush=True)
    run_id, trial_ids = harness.start_run(name=run_name)
    print(f"[full] run_id={run_id} trials_allocated={len(trial_ids)}",
          flush=True)

    print("[full] GetRun → mapping trial_id → task_id", flush=True)
    run_view = harness._harness.get_run(GetRunRequest(run_id=run_id))
    trial_pairs = [(t.task_id, t.trial_id) for t in run_view.trials]
    if args.limit > 0:
        trial_pairs = trial_pairs[: args.limit]

    progress_lock = Lock()
    progress_state = {"done": 0, "errors": 0}
    total = len(trial_pairs)

    def walk_one(pair: tuple[str, str]) -> dict:
        task_id, trial_id = pair
        t0 = time.monotonic()
        finding: dict = {"task_id": task_id, "trial_id": trial_id}
        try:
            started = harness.start_trial(trial_id)
            finding["instruction"] = started.instruction
            finding["harness_url"] = started.harness_url

            if args.dump_workspaces:
                files = walk_and_dump_workspace(
                    started.runtime_client,
                    workspaces_dir / task_id,
                )
            else:
                files = walk_workspace(started.runtime_client)
            finding["workspace_byte_total"] = sum(f.byte_size for f in files)
            finding["workspace_file_count"] = len(files)
            finding["workspace_files"] = [
                {"path": f.path, "sha256": f.sha256, "byte_size": f.byte_size}
                for f in files
            ]

            try:
                started.runtime_client.answer(
                    AnswerRequest(message="alpha", outcome=Outcome.OUTCOME_OK)
                )
                finding["answer_outcome"] = "accepted"
            except Exception as exc:  # noqa: BLE001
                finding["answer_outcome"] = (
                    f"raised:{type(exc).__name__}:{exc!s}"
                )

            score, score_detail = harness.end_task(started)
            finding["trial_score"] = score
            finding["score_detail"] = list(score_detail)
        except Exception as exc:  # noqa: BLE001
            finding["walk_error"] = f"{type(exc).__name__}: {exc!s}"
            finding["walk_traceback"] = traceback.format_exc(limit=4)
            # AGENTS.md process safety: try to end the trial so we don't
            # leave it running on the server.
            try:
                from bitgn.harness_pb2 import EndTrialRequest
                harness._harness.end_trial(EndTrialRequest(trial_id=trial_id))
                finding["recovery_end_trial"] = "ok"
            except Exception as exc2:  # noqa: BLE001
                finding["recovery_end_trial"] = (
                    f"failed:{type(exc2).__name__}:{exc2!s}"
                )
        finding["walk_seconds"] = round(time.monotonic() - t0, 2)
        # Persist per-trial walk record immediately (resilient to crashes).
        (trials_dir / f"{task_id}.json").write_text(
            json.dumps(finding, indent=2, sort_keys=True)
        )
        return finding

    print(f"[full] walking {total} trials with {args.max_workers} workers…",
          flush=True)
    findings: list[dict] = []
    walk_t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        for fut in as_completed(pool.submit(walk_one, p) for p in trial_pairs):
            f = fut.result()
            findings.append(f)
            with progress_lock:
                progress_state["done"] += 1
                if "walk_error" in f:
                    progress_state["errors"] += 1
                done = progress_state["done"]
                errors = progress_state["errors"]
            score_disp = f.get("trial_score", "—")
            det_first = (f.get("score_detail") or [""])[0][:60]
            print(f"[full] ({done}/{total}) {f['task_id']} "
                  f"{f['walk_seconds']}s files={f.get('workspace_file_count', '?')} "
                  f"score={score_disp} detail={det_first!r}", flush=True)

    walk_errors = progress_state["errors"]
    walk_elapsed = time.monotonic() - walk_t0
    print(f"[full] walk loop done in {walk_elapsed:.1f}s. "
          f"errors={walk_errors}/{total}", flush=True)

    # Phase 2: scrape harness URL transcripts after the run loop, parallel.
    print(f"[full] scraping {len(findings)} transcripts with {args.max_workers} workers…",
          flush=True)
    transcript_errors = 0
    transcript_lock = Lock()

    def fetch_one(finding: dict) -> dict:
        url = finding.get("harness_url")
        if not url:
            return finding
        try:
            dump = fetch_trial_data(url)
            transcript = dump.to_dict()
            finding["transcript"] = {
                "log_count": transcript["log_count"],
                "command_count": len(transcript["commands"]),
                "submitted_answer": transcript["submitted_answer"],
                "grader": transcript["grader"],
            }
            per_trial_path = trials_dir / f"{finding['task_id']}.json"
            data = json.loads(per_trial_path.read_text())
            data["transcript_full"] = transcript
            per_trial_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            finding["transcript_error"] = f"{type(exc).__name__}: {exc!s}"
        return finding

    transcript_t0 = time.monotonic()
    transcript_done = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        for fut in as_completed(pool.submit(fetch_one, f) for f in findings):
            f = fut.result()
            with transcript_lock:
                transcript_done += 1
                if "transcript_error" in f:
                    transcript_errors += 1
                done = transcript_done
                errors = transcript_errors
            if "transcript_error" in f:
                print(f"[full] ({done}/{len(findings)}) {f['task_id']} "
                      f"ERROR: {f['transcript_error']}", flush=True)
            elif done % 10 == 0 or done == len(findings):
                t = f.get("transcript", {})
                print(f"[full] ({done}/{len(findings)}) {f['task_id']} "
                      f"logs={t.get('log_count', '?')} "
                      f"cmds={t.get('command_count', '?')}", flush=True)

    transcript_elapsed = time.monotonic() - transcript_t0
    print(f"[full] transcript loop done in {transcript_elapsed:.1f}s. "
          f"errors={transcript_errors}/{len(findings)}", flush=True)

    summary = {
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(tz=timezone.utc).isoformat(),
        "benchmark_id": args.benchmark_id,
        "run_id": run_id,
        "run_name": run_name,
        "trials_in_run": len(trial_ids),
        "trials_walked": len(trial_pairs),
        "walk_errors": walk_errors,
        "transcript_errors": transcript_errors,
        "submit_run_called": False,
        "trials": [
            {
                "task_id": f["task_id"],
                "trial_id": f["trial_id"],
                "harness_url": f.get("harness_url"),
                "trial_score": f.get("trial_score"),
                "score_detail": f.get("score_detail"),
                "workspace_file_count": f.get("workspace_file_count"),
                "workspace_byte_total": f.get("workspace_byte_total"),
                "walk_seconds": f.get("walk_seconds"),
                "walk_error": f.get("walk_error"),
                "transcript_error": f.get("transcript_error"),
            }
            for f in findings
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"[full] wrote {summary_path}", flush=True)
    print(f"[full] dashboard run_id={run_id} name={run_name!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
