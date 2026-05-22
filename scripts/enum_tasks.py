#!/usr/bin/env python3
"""Cheap PROD enumeration: open every trial, capture task_id +
instruction, close with NONE_CLARIFICATION. No probing.

Use this to detect added/changed tasks on bitgn/ecom1-dev cheaply
(no LLM calls, no large reads).

Output: artifacts/enum/<ts>/{manifest.json,trials/<task>.json}
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    StartRunRequest,
    StartTrialRequest,
    SubmitRunRequest,
)
from bitgn.vm.ecom import ecom_pb2
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
from connectrpc.interceptor import MetadataInterceptorSync


class _Auth(MetadataInterceptorSync):
    def __init__(self, key: str) -> None:
        self._key = key

    def on_start_sync(self, ctx) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._key}"


def main() -> int:
    api_key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")
    interceptors = (_Auth(api_key),)
    harness = HarnessServiceClientSync(base, interceptors=interceptors)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("artifacts/enum") / f"enum_{ts}"
    out.mkdir(parents=True, exist_ok=True)

    import subprocess
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"]
    ).decode().strip()
    run_name = f"@ai_engineer_helper DEV-ECOM1 {sha} enum-{ts}"

    print(f"[enum] benchmark={bench}  out={out}", flush=True)
    print(f"[enum] run name: {run_name}", flush=True)
    run = harness.start_run(StartRunRequest(
        benchmark_id=bench, name=run_name, api_key=api_key,
    ))
    trial_ids = list(run.trial_ids)
    print(f"[enum] trial_ids={len(trial_ids)}", flush=True)

    rows: list[dict] = []
    for i, tid in enumerate(trial_ids, 1):
        try:
            started = harness.start_trial(StartTrialRequest(trial_id=tid))
            task_id = started.task_id
            instruction = started.instruction
            harness_url = started.harness_url
            rows.append({
                "task_id": task_id,
                "trial_id": tid,
                "instruction": instruction,
                "harness_url": harness_url,
            })
            (out / "trials").mkdir(parents=True, exist_ok=True)
            (out / "trials" / f"{task_id}.json").write_text(
                json.dumps({
                    "task_id": task_id,
                    "trial_id": tid,
                    "instruction": instruction,
                    "harness_url": harness_url,
                }, indent=2),
                encoding="utf-8",
            )
            print(f"  [{i:>2}/{len(trial_ids)}] {task_id}  {instruction[:80]!r}",
                  flush=True)
            vm = EcomRuntimeClientSync(harness_url, interceptors=interceptors)
            try:
                vm.answer(ecom_pb2.AnswerRequest(
                    message="enumeration probe — no answer attempted",
                    outcome=ecom_pb2.Outcome.OUTCOME_NONE_CLARIFICATION,
                    refs=[],
                ))
            except Exception as exc:
                print(f"     answer failed: {exc}", flush=True)
        except Exception as exc:
            print(f"  [{i:>2}/{len(trial_ids)}] start_trial failed: {exc}",
                  flush=True)
        finally:
            try:
                harness.end_trial(EndTrialRequest(trial_id=tid))
            except Exception as exc:
                print(f"     end_trial failed: {exc}", flush=True)

    try:
        harness.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
    except Exception as exc:
        print(f"[enum] submit_run failed: {exc}", flush=True)

    (out / "manifest.json").write_text(
        json.dumps({
            "benchmark": bench,
            "run_id": run.run_id,
            "ts": ts,
            "task_ids": [r["task_id"] for r in rows],
            "trials": rows,
        }, indent=2),
        encoding="utf-8",
    )
    task_ids = sorted({r["task_id"] for r in rows})
    print(f"[enum] DONE.  unique task_ids: {len(task_ids)}", flush=True)
    print(f"[enum] task list: {task_ids}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
