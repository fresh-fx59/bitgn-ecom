#!/usr/bin/env python3
"""Probe v3: confirm /bin/jq FILE-ARG form + bare array iteration on a
task whose workspace actually exposes /proc/*.json records.

Scans trials until it finds one with json records, then runs the
remaining open questions:
  - file-arg form:  jq -r ".field" /proc/<ns>/<id>.json
  - bare array iteration (no pipe): .lines[].sku , .lines[] , .lines[0]
  - stdin sentinel:  jq -r ".status" -
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
                  "lines": [{"sku": "PWR-1", "qty": 1}, {"sku": "PWR-2", "qty": 3}]})


def _exec(client, args, stdin=""):
    resp = client.exec(E.ExecRequest(path="/bin/jq", stdin=stdin, args=list(args)))
    return {"args": args, "exit_code": getattr(resp, "exit_code", None),
            "stdout": getattr(resp, "stdout", ""), "stderr": getattr(resp, "stderr", "")}


def _find_json(client, root="/"):
    try:
        resp = client.find(E.FindRequest(root=root, name="*.json", kind="", limit=20))
        return list(getattr(resp, "paths", []) or [])
    except Exception as e:
        return [f"__ERR__ {e}"]


def _tree(client, root="/", level=2):
    try:
        resp = client.tree(E.TreeRequest(root=root, level=level))
        return getattr(resp, "text", "") or str(resp)[:500]
    except Exception as e:
        return f"__ERR__ {e}"


def _read(client, path):
    try:
        return (getattr(client.read(E.ReadRequest(path=path)), "content", "") or "")[:600]
    except Exception as e:
        return f"__ERR__ {e}"


def main() -> int:
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set", file=sys.stderr); return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    harness = H.BitgnHarness.from_env(benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key)

    out = {"benchmark": benchmark, "scan": [], "results": []}
    rid, trial_ids = harness.start_run(name="jq-probe3")
    out["run_id"] = rid
    chosen = None
    try:
        for idx in list(range(8, 20)) + list(range(0, 8)):
            if idx >= len(trial_ids):
                continue
            started = harness.start_trial(trial_ids[idx])
            client = started.runtime_client
            jsons = _find_json(client, "/")
            real = [p for p in jsons if isinstance(p, str) and p.endswith(".json")]
            out["scan"].append({"idx": idx, "task": started.task_id,
                                "instr": (started.instruction or "")[:70], "json_found": real[:5]})
            if real:
                chosen = {"task": started.task_id, "client": client, "file": real[0], "all": real[:10]}
                out["chosen_task"] = started.task_id
                out["tree"] = _tree(client, "/", 2)
                break
            # close this unused trial
            try:
                client.answer(E.AnswerRequest(outcome=E.OUTCOME_NONE_CLARIFICATION, message="scan", refs=[]))
            except Exception:
                pass

        if chosen:
            client = chosen["client"]
            f = chosen["file"]
            out["file"] = f
            out["file_content_head"] = _read(client, f)
            # FILE-ARG battery
            out["results"].append(("file-arg keys", _exec(client, ["-r", "keys", f])))
            out["results"].append(("file-arg identity", _exec(client, ["-r", ".", f])))
            # try to read a top field from the actual file: discover first key
            head = out["file_content_head"]
            try:
                obj = json.loads(_read(client, f))
                k0 = next(iter(obj.keys())) if isinstance(obj, dict) else None
            except Exception:
                k0 = None
            if k0:
                out["results"].append((f"file-arg .{k0}", _exec(client, ["-r", f".{k0}", f])))
            # bare array iteration (no pipe) on stdin
            out["results"].append(("bare .lines[].sku", _exec(client, ["-r", ".lines[].sku"], DOC)))
            out["results"].append(("bare .lines[]", _exec(client, ["-r", ".lines[]"], DOC)))
            out["results"].append(("bare .lines[0]", _exec(client, ["-r", ".lines[0]"], DOC)))
            out["results"].append(("stdin sentinel -", _exec(client, ["-r", ".status", "-"], DOC)))
            # close
            try:
                client.answer(E.AnswerRequest(outcome=E.OUTCOME_NONE_CLARIFICATION, message="jq file probe", refs=[]))
            except Exception:
                pass
    finally:
        try:
            harness.submit_run(rid, force=True)
        except Exception as e:
            print(f"submit err: {e}", file=sys.stderr)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    opath = Path("artifacts/prod_explore") / f"jq_probe3_{ts}.json"
    opath.write_text(json.dumps(out, indent=2, default=str))
    print("\n=== scan ===")
    for s in out["scan"]:
        print(f"  idx={s['idx']:2} {s['task']:5} json={s['json_found'][:3]}  | {s['instr']}")
    print(f"\nchosen task={out.get('chosen_task')} file={out.get('file')}")
    print("file head:", (out.get("file_content_head") or "")[:200].replace("\n", "\\n"))
    def strip(s): return (s or "").replace("PowerTools E-Commerce OS jq\n", "<B>").replace("\n", "\\n")
    print("\n=== results ===")
    for label, r in out["results"]:
        ok = "OK " if r["exit_code"] == 0 else "ERR"
        print(f"  [{ok}] {label:24} -> {strip(r['stdout'])[:70]!r}  err={strip(r['stderr'])[:45]!r}")
    print(f"\nfull dump: {opath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
