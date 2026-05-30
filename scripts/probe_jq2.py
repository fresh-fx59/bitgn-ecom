#!/usr/bin/env python3
"""Probe v2: map the PowerTools-OS /bin/jq FILTER LANGUAGE.

v1 established: custom binary, usage `jq [-r|--raw-output] <filter> [path|-]`,
banner line always prepended, only -r supported, reads stdin by default.

This probe tests which filter constructs actually work, using a rich
record on stdin (no /proc dependency) plus a real /proc/catalog file
(present in a SKU task's workspace). Single run to stay economical.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import bitgn_contest_agent.harness as H
import bitgn.vm.ecom.ecom_pb2 as E

DOC = json.dumps({
    "status": "paid",
    "amount": 120,
    "fee": 5,
    "three_ds": {"recovery_allowed": True, "attempts": 2},
    "lines": [
        {"sku": "PWR-1", "qty": 1, "price": 10},
        {"sku": "PWR-2", "qty": 3, "price": 20},
        {"sku": "PWR-3", "qty": 5, "price": 7},
    ],
    "policy": {"max_discount_pct": 7, "blocked": False},
})

# (label, args, stdin)  — stdin="" means rely on file arg already in args
FILTERS = [
    ("identity",                ["-r", "."], DOC),
    ("field",                   ["-r", ".status"], DOC),
    ("nested bool",             ["-r", ".three_ds.recovery_allowed"], DOC),
    ("arithmetic .amount-.fee", ["-r", ".amount - .fee"], DOC),
    ("array index",             ["-r", ".lines[0].sku"], DOC),
    ("iterate pipe field",      ["-r", ".lines[] | .sku"], DOC),
    ("length",                  ["-r", ".lines | length"], DOC),
    ("select gt",               ["-r", ".lines[] | select(.qty > 2) | .sku"], DOC),
    ("map add (sum qty)",       ["-r", "[.lines[].qty] | add"], DOC),
    ("comparison bool",         ["-r", ".lines[1].qty > 2"], DOC),
    ("has",                     ["-r", "has(\"status\")"], DOC),
    ("keys",                    ["-r", "keys"], DOC),
    ("default //",              ["-r", ".missing // \"none\""], DOC),
    ("type",                    ["-r", ".status | type"], DOC),
    ("string interp",           ["-r", "\"st=\\(.status)\""], DOC),
    ("not flag --raw-output",   ["--raw-output", ".status"], DOC),
    ("no -r (string quoting)",  [".status"], DOC),
    ("multiply price*qty",      ["-r", ".lines[] | .price * .qty"], DOC),
    ("tostring",                ["-r", ".amount | tostring"], DOC),
    ("group/complex select+field", ["-r", "[.lines[] | select(.qty>=3) | .sku]"], DOC),
]


def _exec(client, args, stdin=""):
    resp = client.exec(E.ExecRequest(path="/bin/jq", stdin=stdin, args=list(args)))
    return {"args": args, "exit_code": getattr(resp, "exit_code", None),
            "stdout": getattr(resp, "stdout", ""), "stderr": getattr(resp, "stderr", "")}


def _list(client, path):
    try:
        resp = client.list(E.ListRequest(path=path))
        return list(getattr(resp, "paths", []) or [])
    except Exception as e:
        return [f"__ERR__ {e}"]


def main() -> int:
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set", file=sys.stderr); return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    harness = H.BitgnHarness.from_env(benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key)

    out = {"benchmark": benchmark, "results": []}
    rid, trial_ids = harness.start_run(name="jq-probe2")
    out["run_id"] = rid
    started = harness.start_trial(trial_ids[0])
    out["task"] = started.task_id
    client = started.runtime_client
    try:
        # filter-language battery (stdin)
        for label, args, stdin in FILTERS:
            r = _exec(client, args, stdin)
            out["results"].append((label, r))

        # real /proc file (file-arg form). discover what exists in this workspace
        for d in ("/proc", "/proc/catalog", "/proc/payments", "/proc/baskets"):
            out.setdefault("listings", {})[d] = _list(client, d)[:30]
        cat = out["listings"].get("/proc/catalog", [])
        jsons = [p for p in cat if str(p).endswith(".json")]
        if jsons:
            f = jsons[0]
            out["file_arg_test_path"] = f
            out["results"].append((f"FILE-ARG keys {f}", _exec(client, ["-r", "keys", f])))
            out["results"].append((f"FILE-ARG dash-stdin", _exec(client, ["-r", ".status", "-"], DOC)))
    finally:
        try:
            started.runtime_client.answer(E.AnswerRequest(
                outcome=E.OUTCOME_NONE_CLARIFICATION, message="jq filter probe", refs=[]))
        except Exception as e:
            print(f"close err: {e}", file=sys.stderr)
        try:
            harness.submit_run(rid, force=True)
        except Exception as e:
            print(f"submit err: {e}", file=sys.stderr)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    opath = Path("artifacts/prod_explore") / f"jq_probe2_{ts}.json"
    opath.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n=== jq filter-language probe (task={out.get('task')}) ===")
    def strip(s):  # drop the banner line for readability
        s = s or ""
        return s.replace("PowerTools E-Commerce OS jq\n", "<BANNER>").replace("\n", "\\n")
    for label, r in out["results"]:
        ok = "OK " if r["exit_code"] == 0 else "ERR"
        print(f"  [{ok}] {label:32} -> out={strip(r['stdout'])[:60]!r}  err={strip(r['stderr'])[:55]!r}")
    print("\nlistings:")
    for d, v in out.get("listings", {}).items():
        print(f"  {d}: {len(v)} -> {v[:5]}")
    print(f"\nfull dump: {opath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
