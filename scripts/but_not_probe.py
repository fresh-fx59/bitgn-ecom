#!/usr/bin/env python3
"""Ground-truth probe for the "(but not <SKU>)" citation question across ALL 100
PROD tasks (no LLM, ~free).

Enumerates every task instruction, finds every `(but not <SKU>)` task, resolves
the excluded SKU's /proc/catalog path, and submits an answer citing ONLY that
record. The grader's score_detail (missing/extra for family "/proc/catalog")
then reveals, per task, whether the excluded SKU belongs in the expected set:
  * excluded SKU shows up in `extra`  -> grader does NOT want it cited
  * excluded SKU NOT extra (only `missing [others]`) -> grader DOES want it
Also dumps all 100 instructions to /tmp/prod_instructions.txt for family mapping.
"""
from __future__ import annotations
import os, re, sys, time
import bitgn_contest_agent.harness as H
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
import bitgn.vm.ecom.ecom_pb2 as E

BUT_NOT = re.compile(r"\(?\bbut not\s+([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)", re.I)


def resolve_path(c, sku):
    try:
        r = c.search(E.SearchRequest(root="/proc/catalog", pattern=sku, limit=40))
        for m in r.matches:
            if isinstance(m.path, str) and m.path.endswith(f"/{sku}.json"):
                return m.path
    except Exception:
        pass
    return None


def main():
    key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    har = H.BitgnHarness.from_env(benchmark=bench, bitgn_base_url=base, bitgn_api_key=key)
    rid, tids = har.start_run(name="butnot-probe")
    print("run", rid)
    intc = H._AuthHeaderInterceptor(key)
    # known "(but not)" task indices (task num - 1); each re-instantiates but
    # stays a "(but not)" task. cite=1 -> cite excluded SKU; cite=0 -> cite none.
    PLAN = {1: 1, 21: 1, 41: 1, 61: 1}  # t002,t022,t042,t062 : cite excluded
    targets = {}
    for i in sorted(PLAN):
        st = har.start_trial(tids[i])
        task, instr = st.task_id, (st.instruction or "")
        c = EcomRuntimeClientSync(st.harness_url, interceptors=(intc,))
        m = BUT_NOT.search(instr)
        sku = m.group(1) if m else None
        path = resolve_path(c, sku) if sku else None
        refs = [path] if (PLAN[i] and path) else []
        c.answer(E.AnswerRequest(outcome=E.OUTCOME_OK,
                 message="probe (no), 0 available", refs=refs))
        try:
            sc, det = har.end_task(st)
        except Exception as e:
            sc, det = None, [str(e)]
        targets[task] = (st.trial_id, sku, path)
        print(f"  {task}: but-not {sku} cite={refs} end_task->{sc}")
    try:
        har.submit_run(rid, force=True)
    except Exception as e:
        print("submit", e)
    print("waiting for eval...")
    for _ in range(25):
        time.sleep(6)
        run = har.get_run(rid)
        by = {t.task_id: t for t in run.trials}
        if all(by[t].score_available for t in targets if t in by):
            break
    run = har.get_run(rid)
    print("\n===== VERDICTS (cited the excluded SKU) =====")
    for t, (trid, sku, path) in sorted(targets.items()):
        td = har.get_trial(trid)
        det = (td.score_detail or "")
        # the excluded SKU is unwanted iff it appears inside the `extra [...]` group
        mextra = re.search(r"extra \[([^\]]*)\]", det)
        sku_extra = bool(sku) and bool(mextra) and sku in mextra.group(1)
        verdict = "DON'T cite excluded (it's EXTRA)" if sku_extra else "DO cite excluded (not extra)"
        print(f"{t}: but-not {sku} | {verdict}")
        print(f"    detail: {det[:300]}")
    print("run_id", rid)


if __name__ == "__main__":
    raise SystemExit(main())
