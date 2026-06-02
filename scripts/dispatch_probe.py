#!/usr/bin/env python3
"""Ground-truth probe for the PROD dispatch-wave efficiency ceiling
(t004/t014/t024/t044/t064).

Submits DIFFERENT deterministically-constructed dispatch plans (no LLM) to the 5
dispatch instances in one run via the runtime answer() RPC, then reads the
grader's score_detail (delivered/late/missed, money, transport, late/miss
penalties, gain, gain_max, efficiency%). This isolates plan-quality sensitivity
from per-world variance and tells us:

  * the FLOOR (empty/missed plan) and a BAD valid plan vs GOOD/optimal plans,
  * whether a reliability-optimal plan can push efficiency materially above the
    ~0.82 the agent gets — i.e. is the reference the no-delay optimum (1.0
    unreachable) or the stochastic optimum (1.0 reachable).

Costs only rate-limit slots (~no $). One run = 5 dispatch instances.

Usage:
    .venv/bin/python scripts/dispatch_probe.py [--assign t004=reliable,t014=mincost,...]

Variants: ev, mincost, reliable, fastest, bad, empty.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict

import bitgn_contest_agent.harness as H
from bitgn_contest_agent import dispatch_planner as dp
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
import bitgn.vm.ecom.ecom_pb2 as E

DISPATCH = {"t004": 3, "t014": 13, "t024": 23, "t044": 43, "t064": 63}
PKG_RE = re.compile(r"Packages:\s*(\S+)", re.IGNORECASE)
LANE_RE = re.compile(r"Lanes:\s*(\S+)", re.IGNORECASE)
WAVE_RE = re.compile(r"(/\S*dispatch\S*\.md)\b", re.IGNORECASE)

DEFAULT_ASSIGN = {
    "t004": "reliable",
    "t014": "mincost",
    "t024": "ev",
    "t044": "bad",
    "t064": "empty",
}


def _unwrap(content: str) -> str:
    """`read` returns a JSON body with a `content` field holding file bytes."""
    if not content:
        return ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
            return parsed["content"]
    except (ValueError, AttributeError):
        pass
    return content


def build_plan(pkgs, lanes, variant: str) -> dict:
    adj = defaultdict(list)
    for ln in lanes:
        adj[ln.from_].append(ln)
    ordered = sorted(pkgs, key=lambda p: (p.due_time, -p.margin_cents, p.package_id))
    prio = {p.package_id: i + 1 for i, p in enumerate(ordered)}

    if variant == "ev":
        plan = dp.plan(pkgs, lanes)
        return plan

    assigns = []
    for p in pkgs:
        routes = dp._enumerate_routes(p.from_store_id, p.to_store_id, adj)
        route = []
        if variant == "empty" or not routes:
            route = []
        elif variant == "mincost":
            ontime = [r for r in routes if dp._route_eta(r) <= p.due_time] or routes
            route = min(ontime, key=lambda r: (dp._route_cost(r), dp._route_eta(r), len(r)))
        elif variant == "reliable":
            ontime = [r for r in routes if dp._route_eta(r) <= p.due_time] or routes
            route = min(
                ontime,
                key=lambda r: (
                    sum(dp._expected_delay(l) for l in r),  # least expected delay
                    dp._route_eta(r),                        # most slack
                    dp._route_cost(r),                       # cheapest
                    len(r),
                ),
            )
        elif variant == "fastest":
            route = min(routes, key=lambda r: (dp._route_eta(r), dp._route_cost(r), len(r)))
        elif variant == "bad":
            route = max(routes, key=lambda r: (dp._route_eta(r), dp._route_cost(r), len(r)))
        else:
            raise SystemExit(f"unknown variant {variant}")
        assigns.append(
            {
                "package_id": p.package_id,
                "route": [l.lane_id for l in route],
                "priority": prio[p.package_id],
            }
        )
    return {"assignments": assigns}


def main() -> int:
    assign = dict(DEFAULT_ASSIGN)
    if "--assign" in sys.argv:
        spec = sys.argv[sys.argv.index("--assign") + 1]
        for kv in spec.split(","):
            k, v = kv.split("=")
            assign[k.strip()] = v.strip()

    key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    har = H.BitgnHarness.from_env(benchmark=bench, bitgn_base_url=base, bitgn_api_key=key)
    rid, tids = har.start_run(name="dispatch-probe")
    print("run", rid, "assign", assign)
    intc = H._AuthHeaderInterceptor(key)
    idx2task = {v: k for k, v in DISPATCH.items()}
    submitted = {}
    try:
        maxidx = max(DISPATCH.values())
        for i, tid in enumerate(tids):
            if i > maxidx:
                break
            if i not in idx2task:
                continue
            task = idx2task[i]
            variant = assign.get(task, "ev")
            st = har.start_trial(tid)
            c = EcomRuntimeClientSync(st.harness_url, interceptors=(intc,))
            instr = st.instruction or ""
            wm = WAVE_RE.search(instr)
            if not wm:
                print(f"  {task}: NO wave path in instruction:\n   {instr[:160]}")
                continue
            wave_md = _unwrap(getattr(c.read(E.ReadRequest(path=wm.group(1))), "content", "") or "")
            mp = PKG_RE.search(wave_md)
            ml = LANE_RE.search(wave_md)
            if not mp or not ml:
                print(f"  {task}: cannot parse package/lane paths from wave md:\n{wave_md[:200]}")
                continue
            pkg_tsv = _unwrap(getattr(c.read(E.ReadRequest(path=mp.group(1).strip().rstrip('.,;'))), "content", "") or "")
            lane_tsv = _unwrap(getattr(c.read(E.ReadRequest(path=ml.group(1).strip().rstrip('.,;'))), "content", "") or "")
            pkgs = dp.parse_packages(pkg_tsv)
            lanes = dp.parse_lanes(lane_tsv)
            plan = build_plan(pkgs, lanes, variant)
            n_routed = sum(1 for a in plan["assignments"] if a["route"])
            msg = json.dumps(plan, separators=(",", ":"))
            c.answer(E.AnswerRequest(outcome=E.OUTCOME_OK, message=msg, refs=[]))
            try:
                sc, det = har.end_task(st)
            except Exception as e:
                sc, det = None, [f"end_task err {e}"]
            print(f"  {task} [{variant}] {len(pkgs)}pkg {len(lanes)}lane routed={n_routed} -> score={sc}")
            print(f"     detail={det}")
            submitted[task] = (st.trial_id, variant)
    finally:
        try:
            har.submit_run(rid, force=True)
        except Exception as e:
            print("submit err", e)
    print("waiting for eval...")
    for _ in range(20):
        time.sleep(6)
        run = har.get_run(rid)
        by = {th.task_id: th for th in run.trials}
        if all(by[t].score_available for t in submitted if t in by):
            break
    run = har.get_run(rid)
    print("=== FINAL ===")
    for th in run.trials:
        if th.task_id in submitted:
            sd = ""
            try:
                sd = (har.get_trial(th.trial_id).score_detail or "")
            except Exception:
                pass
            v = submitted[th.task_id][1]
            print(f"== {th.task_id} [{v}]: score={th.score if th.score_available else 'NA'}")
            print(f"   {sd}")
    print("run_id", rid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
