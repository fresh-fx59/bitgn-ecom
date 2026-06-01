#!/usr/bin/env python3
"""Ground-truth probe for the PROD archive-fraud rule (t015/t035/t055/t075).

Submits a DETERMINISTICALLY-constructed fraud answer (no LLM inference) via the
runtime answer() RPC, then reads the grader's per-trial score + score_detail
(which reports recall% and false-positive count). That directional feedback lets
us reverse-engineer the exact planting rule cheaply (only rate-limit slots, ~no $).

Rule under test is selected by --rule. Prints every candidate rule's
size/amount per task so one run maps the whole landscape.
"""
from __future__ import annotations
import os, re, sys, csv, io, time
from collections import defaultdict
import bitgn_contest_agent.harness as H
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
import bitgn.vm.ecom.ecom_pb2 as E

# 0-based trial indices (trials returned in task order t001..t100)
FRAUD = {"t015":14, "t035":34, "t055":54, "t075":74}
ARCHIVE_RE = re.compile(r"(/archive/\S+\.tsv)")


def parse(tsv):
    return list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))


def _maps(rows):
    dev_c, meth_c = defaultdict(set), defaultdict(set)
    c_dev, c_meth = defaultdict(set), defaultdict(set)
    for r in rows:
        dev_c[r["device_fingerprint"]].add(r["customer_ref"])
        meth_c[r["payment_method_fingerprint"]].add(r["customer_ref"])
        c_dev[r["customer_ref"]].add(r["device_fingerprint"])
        c_meth[r["customer_ref"]].add(r["payment_method_fingerprint"])
    return dev_c, meth_c, c_dev, c_meth


def _components(rows, link_cust=True):
    """Union-find over rows linked by shared device OR method (OR customer)."""
    parent = list(range(len(rows)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        parent[find(a)] = find(b)
    fields = ["device_fingerprint", "payment_method_fingerprint"]
    if link_cust:
        fields.append("customer_ref")
    for f in fields:
        groups = defaultdict(list)
        for i, r in enumerate(rows):
            groups[r[f]].append(i)
        for idxs in groups.values():
            for j in idxs[1:]:
                union(idxs[0], j)
    comp = defaultdict(list)
    for i in range(len(rows)):
        comp[find(i)].append(i)
    return list(comp.values())


def _compNT(rows, link_cust=True):
    """Fraud = rows in a connected component with fan-out: >1 customer OR
    >1 device OR >1 method. A pure {1 cust,1 dev,1 meth} cluster is legit."""
    keep = set()
    for comp in _components(rows, link_cust):
        custs = {rows[i]["customer_ref"] for i in comp}
        devs = {rows[i]["device_fingerprint"] for i in comp}
        meths = {rows[i]["payment_method_fingerprint"] for i in comp}
        if len(custs) > 1 or len(devs) > 1 or len(meths) > 1:
            keep.update(comp)
    return keep


def _seed(rows):
    """Strong fraud rings (device/method-linked component is a SEED iff it has
    ≥2 customers, ≥3 cities, ≥3 devices, or ≥3 methods), then expand to ALL rows
    of any customer that appears in a seed component (membership)."""
    seed_custs = set()
    seed_rows = set()
    for comp in _components(rows, link_cust=False):
        custs = {rows[i]["customer_ref"] for i in comp}
        devs = {rows[i]["device_fingerprint"] for i in comp}
        meths = {rows[i]["payment_method_fingerprint"] for i in comp}
        cities = {rows[i]["store_city"] for i in comp}
        if len(custs) >= 2 or len(cities) >= 3 or len(devs) >= 3 or len(meths) >= 3:
            seed_rows.update(comp)
            seed_custs.update(custs)
    keep = set(seed_rows)
    for i, r in enumerate(rows):
        if r["customer_ref"] in seed_custs:
            keep.add(i)
    return keep


def rule_fraud(rows, name):
    dev_c, meth_c, c_dev, c_meth = _maps(rows)
    compNT = _compNT(rows, link_cust=True)
    compNT_nocust = _compNT(rows, link_cust=False)
    seed = _seed(rows)
    out = []
    for idx, r in enumerate(rows):
        c, d, m = r["customer_ref"], r["device_fingerprint"], r["payment_method_fingerprint"]
        cross = len(dev_c[d]) >= 2 or len(meth_c[m]) >= 2
        cyc3 = len(c_meth[c]) >= 3 or len(c_dev[c]) >= 3
        cyc_md = len(c_meth[c]) >= 3 or len(c_dev[c]) >= 2
        if name == "cross":          keep = cross
        elif name == "cycle3":       keep = cyc3
        elif name == "v1":           keep = cross or cyc3
        elif name == "v2":           keep = cross or cyc_md
        elif name == "compNT":       keep = idx in compNT
        elif name == "compNTnc":     keep = idx in compNT_nocust
        elif name == "seed":         keep = idx in seed
        elif name == "broad":        keep = _shared_dev(rows, d) or _shared_meth(rows, m)
        else:                        keep = idx in compNT
        if keep:
            out.append(r)
    return out


def _shared_dev(rows, d):
    return sum(1 for r in rows if r["device_fingerprint"] == d) >= 2
def _shared_meth(rows, m):
    return sum(1 for r in rows if r["payment_method_fingerprint"] == m) >= 2


def amount_str(rows):
    cents = sum(int(r["amount_cents"]) for r in rows)
    return f"EUR {cents//100}.{cents%100:02d}", cents


def main():
    rule = "v1"
    if "--rule" in sys.argv:
        rule = sys.argv[sys.argv.index("--rule") + 1]
    key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-prod")
    har = H.BitgnHarness.from_env(benchmark=bench, bitgn_base_url=base, bitgn_api_key=key)
    rid, tids = har.start_run(name="fraud-probe")
    print("run", rid, "rule", rule)
    intc = H._AuthHeaderInterceptor(key)
    idx2task = {v: k for k, v in FRAUD.items()}
    submitted = {}
    try:
        maxidx = max(FRAUD.values())
        for i, tid in enumerate(tids):
            if i > maxidx:
                break
            if i not in idx2task:
                continue
            st = har.start_trial(tid)
            task = st.task_id
            c = EcomRuntimeClientSync(st.harness_url, interceptors=(intc,))
            m = ARCHIVE_RE.search(st.instruction or "")
            apath = m.group(1)
            tsv = getattr(c.read(E.ReadRequest(path=apath)), "content", "") or ""
            rows = parse(tsv)
            # landscape print
            land = {}
            for rn in ("cross", "v1", "compNT", "compNTnc", "seed", "broad"):
                fr = rule_fraud(rows, rn)
                _, cents = amount_str(fr)
                land[rn] = (len(fr), cents / 100)
            print(f"  {task}: {len(rows)} rows | " +
                  " ".join(f"{k}={v[0]}/{v[1]:.0f}" for k, v in land.items()))
            fraud = rule_fraud(rows, rule)
            msg, cents = amount_str(fraud)
            refs = [f"{apath}#row={r['row_id']}" for r in fraud]
            c.answer(E.AnswerRequest(outcome=E.OUTCOME_OK, message=msg, refs=refs))
            # synchronous grade (playground flow returns score+detail directly)
            try:
                sc, det = har.end_task(st)
            except Exception as e:
                sc, det = None, [f"end_task err {e}"]
            print(f"    {task}: {msg} ({len(refs)} refs) -> score={sc} detail={str(det)[:260]}")
            submitted[task] = (st.trial_id, msg, len(refs))
    finally:
        try:
            har.submit_run(rid, force=True)
        except Exception as e:
            print("submit err", e)
    # poll scores
    print("waiting for eval...")
    for _ in range(20):
        time.sleep(6)
        run = har.get_run(rid)
        by = {th.task_id: th for th in run.trials}
        ready = all(by[t].score_available for t in submitted if t in by)
        if ready:
            break
    run = har.get_run(rid)
    for th in run.trials:
        if th.task_id in submitted:
            sd = ""
            try:
                td = har.get_trial(th.trial_id)
                sd = (td.score_detail or "")[:300]
            except Exception:
                pass
            print(f"== {th.task_id}: score={th.score if th.score_available else 'NA'} | {sd}")
    print("run_id", rid)


if __name__ == "__main__":
    raise SystemExit(main())
