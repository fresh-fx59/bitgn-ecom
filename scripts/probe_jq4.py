#!/usr/bin/env python3
"""Probe v4 (final): array-iteration grammar + file-arg form on a real
/proc record. Targets the refund task t017 (idx 16) whose workspace
should expose /proc/payments/pay-0014.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import bitgn_contest_agent.harness as H
import bitgn.vm.ecom.ecom_pb2 as E

DOC = json.dumps({"status": "paid",
                  "lines": [{"sku": "PWR-1", "qty": 1}, {"sku": "PWR-2", "qty": 3}],
                  "three_ds": {"recovery_allowed": True}})


def _exec(client, args, stdin=""):
    resp = client.exec(E.ExecRequest(path="/bin/jq", stdin=stdin, args=list(args)))
    return {"args": args, "exit_code": getattr(resp, "exit_code", None),
            "stdout": getattr(resp, "stdout", ""), "stderr": getattr(resp, "stderr", "")}


def _read(client, path):
    try:
        return getattr(client.read(E.ReadRequest(path=path)), "content", "") or ""
    except Exception as e:
        return f"__ERR__ {e}"


def main() -> int:
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set", file=sys.stderr); return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    harness = H.BitgnHarness.from_env(benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key)

    out = {"benchmark": benchmark, "iter_results": [], "file_results": []}
    rid, trial_ids = harness.start_run(name="jq-probe4")
    out["run_id"] = rid
    try:
        started = harness.start_trial(trial_ids[16])  # t017 refund pay-0014
        client = started.runtime_client
        out["task"] = started.task_id
        out["instr"] = (started.instruction or "")[:120]

        # ---- array iteration grammar (stdin, no /proc needed) ----
        for label, filt in [
            ("bare .lines[].sku", ".lines[].sku"),
            ("bare .lines[]", ".lines[]"),
            (".lines", ".lines"),
            (".three_ds", ".three_ds"),
            (".lines[1].sku", ".lines[1].sku"),
        ]:
            out["iter_results"].append((label, _exec(client, ["-r", filt], DOC)))

        # ---- file-arg form on a real payment record ----
        candidates = [
            "/proc/payments/pay-0014.json",
            "/proc/payments/pay-0014",
            "/proc/payments/README.md",
        ]
        # discover from AGENTS.MD / docs what the real path looks like
        out["agents_md_head"] = _read(client, "/AGENTS.MD")[:500]
        found_path = None
        for p in candidates:
            c = _read(client, p)
            ok = not c.startswith("__ERR__") and c.strip() != ""
            out["file_results"].append((f"read {p}", {"ok": ok, "head": c[:160]}))
            if ok and p.endswith(".json"):
                found_path = p
                break
        if found_path:
            out["found_path"] = found_path
            out["file_results"].append((f"jq -r . {found_path}", _exec(client, ["-r", ".", found_path])))
            out["file_results"].append((f"jq -r keys {found_path}", _exec(client, ["-r", "keys", found_path])))
            out["file_results"].append((f"jq -r .status {found_path}", _exec(client, ["-r", ".status", found_path])))

        try:
            client.answer(E.AnswerRequest(outcome=E.OUTCOME_NONE_CLARIFICATION, message="jq probe4", refs=[]))
        except Exception:
            pass
    finally:
        try:
            harness.submit_run(rid, force=True)
        except Exception as e:
            print(f"submit err: {e}", file=sys.stderr)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    opath = Path("artifacts/prod_explore") / f"jq_probe4_{ts}.json"
    opath.write_text(json.dumps(out, indent=2, default=str))

    def strip(s): return (s or "").replace("PowerTools E-Commerce OS jq\n", "<B>").replace("\n", "\\n")
    print(f"\n=== task={out.get('task')} | {out.get('instr')} ===")
    print("--- array iteration grammar ---")
    for label, r in out["iter_results"]:
        ok = "OK " if r["exit_code"] == 0 else "ERR"
        print(f"  [{ok}] {label:22} -> {strip(r['stdout'])[:55]!r}  err={strip(r['stderr'])[:40]!r}")
    print("--- file-arg form ---")
    for label, r in out["file_results"]:
        if "exit_code" in r:
            ok = "OK " if r["exit_code"] == 0 else "ERR"
            print(f"  [{ok}] {label:34} -> {strip(r['stdout'])[:50]!r}")
        else:
            print(f"  [read] {label:34} ok={r['ok']} head={r['head'][:80]!r}")
    print("AGENTS.MD head:", out.get("agents_md_head", "")[:160].replace("\n", " | "))
    print(f"\nfull dump: {opath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
