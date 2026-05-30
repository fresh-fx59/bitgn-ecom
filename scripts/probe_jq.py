#!/usr/bin/env python3
"""Read-only probe of the PROD /bin/jq contract.

Uses the playground flow (start_task) so the trial is invisible to the
leaderboard and does not consume a run slot. Discovers:
  - is /bin/jq present (read stub + list /bin)?
  - --version
  - stdin contract:  jq -r FILTER  (json on stdin)
  - file-arg contract: jq -r FILTER /proc/<file>.json
  - -c / -e flags, compute, missing-field behaviour, exit codes

Dumps everything to artifacts/prod_explore/jq_probe_<ts>.json and prints
a summary. No mutation, no grading.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import bitgn_contest_agent.harness as H
import bitgn.vm.ecom.ecom_pb2 as E


def _exec(client, path, stdin="", args=None):
    resp = client.exec(E.ExecRequest(path=path, stdin=stdin, args=list(args or [])))
    return {
        "path": path,
        "args": list(args or []),
        "stdin": stdin,
        "stdout": getattr(resp, "stdout", ""),
        "exit_code": getattr(resp, "exit_code", None),
        "stderr": getattr(resp, "stderr", ""),
    }


def _read(client, path):
    try:
        resp = client.read(E.ReadRequest(path=path))
        return {
            "path": path,
            "content": (getattr(resp, "content", "") or "")[:400],
            "content_type": getattr(resp, "content_type", ""),
            "sha256": getattr(resp, "sha256", ""),
        }
    except Exception as e:
        return {"path": path, "error": str(e)}


def _list(client, path):
    try:
        resp = client.list(E.ListRequest(path=path))
        return {"path": path, "paths": list(getattr(resp, "paths", []) or [])[:60]}
    except Exception as e:
        return {"path": path, "error": str(e)}


def main() -> int:
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set", file=sys.stderr)
        return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    task_id = os.environ.get("PROBE_TASK", "t001")

    harness = H.BitgnHarness.from_env(
        benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key
    )

    out = {"benchmark": benchmark, "task_id": task_id, "probes": []}
    started = None
    try:
        try:
            started = harness.start_task(task_id)
            out["flow"] = "playground"
        except Exception as e:
            print(f"start_task playground failed ({e}); falling back to run/trial",
                  file=sys.stderr)
            rid, trial_ids = harness.start_run(name="jq-probe")
            out["flow"] = "run"
            out["run_id"] = rid
            started = harness.start_trial(trial_ids[0])
        client = started.runtime_client
        out["resolved_task"] = started.task_id
        out["instruction_head"] = (started.instruction or "")[:160]

        # 1. presence
        out["probes"].append(("list /bin", _list(client, "/bin")))
        out["probes"].append(("read /bin/jq", _read(client, "/bin/jq")))

        # 2. version / no-args behaviour
        out["probes"].append(("jq --version", _exec(client, "/bin/jq", args=["--version"])))
        out["probes"].append(("jq (no args)", _exec(client, "/bin/jq")))

        # 3. stdin contract
        j = '{"name":"hi","n":3,"nested":{"ok":true}}'
        out["probes"].append(("jq -r .name (stdin)", _exec(client, "/bin/jq", stdin=j, args=["-r", ".name"])))
        out["probes"].append(("jq .n+1 (stdin)", _exec(client, "/bin/jq", stdin=j, args=["-c", ".n + 1"])))
        out["probes"].append(("jq -e .missing (stdin)", _exec(client, "/bin/jq", stdin=j, args=["-e", ".missing"])))
        out["probes"].append(("jq .nested.ok (stdin)", _exec(client, "/bin/jq", stdin=j, args=["-r", ".nested.ok"])))

        # 4. file-arg contract — find a real /proc json first
        proc_file = None
        for cand_dir in ("/proc/payments", "/proc/baskets", "/proc/stores"):
            lst = _list(client, cand_dir)
            out["probes"].append((f"list {cand_dir}", lst))
            paths = lst.get("paths") or []
            jsons = [p for p in paths if str(p).endswith(".json")]
            if jsons:
                proc_file = jsons[0]
                break
        out["proc_file_used"] = proc_file
        if proc_file:
            out["probes"].append((f"jq -r . {proc_file} (file arg)",
                                  _exec(client, "/bin/jq", args=["-r", ".", proc_file])))
            out["probes"].append((f"jq keys {proc_file} (file arg)",
                                  _exec(client, "/bin/jq", args=["-c", "keys", proc_file])))
    finally:
        # close cleanly without grading
        try:
            if started is not None:
                started.runtime_client.answer(E.AnswerRequest(
                    outcome=E.OUTCOME_NONE_CLARIFICATION,
                    message="jq contract probe (read-only)",
                    refs=[],
                ))
        except Exception as e:
            print(f"close err: {e}", file=sys.stderr)
        if out.get("flow") == "run" and out.get("run_id"):
            try:
                harness.submit_run(out["run_id"], force=True)
            except Exception as e:
                print(f"submit err: {e}", file=sys.stderr)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    odir = Path("artifacts/prod_explore")
    odir.mkdir(parents=True, exist_ok=True)
    opath = odir / f"jq_probe_{ts}.json"
    opath.write_text(json.dumps(out, indent=2, default=str))

    print(f"\n=== jq probe summary (flow={out.get('flow')}, task={out.get('resolved_task')}) ===")
    for label, res in out["probes"]:
        if "exit_code" in res:
            so = (res.get("stdout") or "").replace("\n", "\\n")[:80]
            se = (res.get("stderr") or "").replace("\n", "\\n")[:80]
            print(f"  [{label}] exit={res['exit_code']} stdout={so!r} stderr={se!r}")
        elif "paths" in res:
            print(f"  [{label}] {len(res['paths'])} paths e.g. {res['paths'][:4]}")
        elif "content" in res:
            print(f"  [{label}] ctype={res.get('content_type')} sha={str(res.get('sha256'))[:12]} content={res.get('content')!r}")
        else:
            print(f"  [{label}] {res}")
    print(f"\nfull dump: {opath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
