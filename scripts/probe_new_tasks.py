#!/usr/bin/env python3
"""Deep-probe only the new (t43/t44 refund) tasks. Reuses
scan_ecom1.probe_trial for the full battery so we can mirror the
workspace into a local ws_snapshot.

Set TARGET_TASKS env to comma-list of task_ids to deep-probe (default
t43,t44). Every other trial is opened and closed immediately to save
cost (we still must close every trial we start to keep dashboard
clean).

Output dir: artifacts/scans/scan_<ts>_new
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

# Reuse machinery from scan_ecom1
sys.path.insert(0, str(Path(__file__).parent))
from scan_ecom1 import (  # type: ignore
    _Auth,
    TrialProbeConfig,
    probe_trial,
)


def main() -> int:
    targets = set(
        (os.environ.get("TARGET_TASKS") or "t43,t44").split(",")
    )
    api_key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")
    interceptors = (_Auth(api_key),)
    harness = HarnessServiceClientSync(base, interceptors=interceptors)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = Path("artifacts/scans") / f"scan_{ts}_new"
    out_root.mkdir(parents=True, exist_ok=True)

    import subprocess
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"]
    ).decode().strip()
    run_name = f"@ai_engineer_helper DEV-ECOM1 {sha} probe-new-{ts}"

    print(f"[probe_new] targets={sorted(targets)}", flush=True)
    print(f"[probe_new] benchmark={bench}  out={out_root}", flush=True)
    run = harness.start_run(StartRunRequest(
        benchmark_id=bench, name=run_name, api_key=api_key,
    ))
    trial_ids = list(run.trial_ids)
    print(f"[probe_new] trial_ids={len(trial_ids)}", flush=True)

    config = TrialProbeConfig(read_every=True, sample_per_dir=999)
    manifest: dict = {"benchmark": bench, "run_id": run.run_id,
                      "ts": ts, "trials": []}

    for i, tid in enumerate(trial_ids, 1):
        try:
            started = harness.start_trial(StartTrialRequest(trial_id=tid))
        except Exception as exc:
            print(f"  [{i}/{len(trial_ids)}] start_trial failed: {exc}",
                  flush=True)
            continue
        task_id = started.task_id
        vm = EcomRuntimeClientSync(started.harness_url,
                                    interceptors=interceptors)
        try:
            if task_id in targets:
                print(f"  [{i}/{len(trial_ids)}] {task_id} → DEEP PROBE",
                      flush=True)
                trial_out = out_root / "trials" / task_id
                summary = probe_trial(
                    vm=vm, out=trial_out,
                    task_id=task_id, trial_id=tid,
                    instruction=started.instruction,
                    harness_url=started.harness_url,
                    config=config,
                )
                manifest["trials"].append({
                    "task_id": task_id, "trial_id": tid,
                    "deep_probed": True,
                    "n_probes": len(summary.get("probes") or []),
                })
            else:
                print(f"  [{i}/{len(trial_ids)}] {task_id} → skip",
                      flush=True)
                manifest["trials"].append({
                    "task_id": task_id, "trial_id": tid,
                    "deep_probed": False,
                })
        finally:
            try:
                vm.answer(ecom_pb2.AnswerRequest(
                    message="probe — no answer attempted",
                    outcome=ecom_pb2.Outcome.OUTCOME_NONE_CLARIFICATION,
                    refs=[],
                ))
            except Exception as exc:
                print(f"    answer failed: {exc}", flush=True)
            try:
                harness.end_trial(EndTrialRequest(trial_id=tid))
            except Exception as exc:
                print(f"    end_trial failed: {exc}", flush=True)

    try:
        harness.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
    except Exception as exc:
        print(f"[probe_new] submit_run failed: {exc}", flush=True)

    (out_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    print(f"[probe_new] DONE. manifest: {out_root}/manifest.json",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
