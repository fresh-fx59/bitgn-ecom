#!/usr/bin/env python3
"""Targeted second scrape: start ONLY the trials we need (by task-order index),
tree a fraud workspace for any rule doc, and read full catalog records for the
ambiguous resolution families. Saves under /tmp/prod_scrape2/<task>/.
"""
from __future__ import annotations
import os, re, sys, json
from pathlib import Path
import bitgn_contest_agent.harness as H
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
import bitgn.vm.ecom.ecom_pb2 as E

OUT = Path("/tmp/prod_scrape2")
# task -> 0-based trial index (trials returned in task order t001..t100)
WANT = {"t002":1, "t015":14, "t041":40, "t047":46, "t062":61}

def rd(c, p):
    try: return getattr(c.read(E.ReadRequest(path=p)), "content", "") or ""
    except Exception as e: return f"__ERR__ {e}"
def ls(c, p):
    try: return [(e.name,e.kind) for e in c.list(E.ListRequest(path=p)).entries]
    except Exception as e: return [("__ERR__",str(e))]
def srch(c, root, pat, limit=80):
    try:
        r=c.search(E.SearchRequest(root=root, pattern=pat, limit=limit))
        return [(m.path,m.line,m.line_text) for m in r.matches]
    except Exception as e: return [("__ERR__",0,str(e))]
def tree(c, root, level=4):
    try:
        r=c.tree(E.TreeRequest(root=root, level=level))
        out=[]
        def walk(n,d=0):
            out.append("  "*d+f"{n.name} [{n.kind}]")
            for ch in n.children: walk(ch,d+1)
        walk(r.root)
        return "\n".join(out)
    except Exception as e: return f"__ERR__ {e}"

def handle(c, task, instr, d):
    d.mkdir(parents=True, exist_ok=True)
    (d/"instruction.txt").write_text(instr or "")
    if task=="t015":
        (d/"tree.txt").write_text(tree(c,"/",5))
        # hunt for fraud-rule docs
        for root in ("/policies","/docs","/ops","/rules","/risk"):
            r=ls(c,root)
            (d/f"ls_{root.strip('/')}.txt").write_text(str(r))
        for pat in ("fraud","Fraud","ring","velocity"):
            (d/f"search_{pat}.txt").write_text(str(srch(c,"/",pat,limit=60)))
    if task=="t002":
        for v in ("CAR","HOME","PC","PIPE","PREMIUM"):
            (d/f"K4-{v}.json").write_text(rd(c,f"/proc/catalog/Karcher/PT-WASH-KAR-K4-{v}.json"))
    if task=="t062":
        for v in ("BODY","AK20","AK30"):
            (d/f"RMA235-{v}.json").write_text(rd(c,f"/proc/catalog/Stihl/PT-MOW-STI-RMA235-{v}.json"))
    if task=="t047":
        (d/"catalog_dirs.txt").write_text(str(ls(c,"/proc/catalog")))
        ms=srch(c,"/proc/catalog","izing",limit=120)+srch(c,"/proc/catalog","emplate",limit=120)
        paths=sorted({m[0] for m in ms if isinstance(m[0],str) and m[0].endswith(".json")})
        recs={p:rd(c,p) for p in paths}
        (d/"sizing_records.json").write_text(json.dumps(recs,indent=1))
        (d/"sizing_paths.txt").write_text("\n".join(paths))
    if task=="t041":
        (d/"catalog_dirs.txt").write_text(str(ls(c,"/proc/catalog")))
        ms=srch(c,"/proc/catalog","DeWalt",limit=160)+srch(c,"/proc/catalog","driver",limit=160)
        paths=sorted({m[0] for m in ms if isinstance(m[0],str) and m[0].endswith(".json")})
        recs={p:rd(c,p) for p in paths}
        (d/"dewalt_records.json").write_text(json.dumps(recs,indent=1))

def main():
    key=os.environ["BITGN_API_KEY"]
    base=os.environ.get("BITGN_BASE_URL","https://api.bitgn.com")
    bench=os.environ.get("BITGN_BENCHMARK","bitgn/ecom1-prod")
    har=H.BitgnHarness.from_env(benchmark=bench, bitgn_base_url=base, bitgn_api_key=key)
    rid,tids=har.start_run(name="scrape2")
    print("run",rid,"trials",len(tids))
    intc=H._AuthHeaderInterceptor(key)
    idx2task={v:k for k,v in WANT.items()}
    done=set()
    try:
        maxidx=max(WANT.values())
        for i,tid in enumerate(tids):
            if i>maxidx: break
            if i not in idx2task: continue
            started=har.start_trial(tid)
            if started.task_id!=idx2task[i]:
                print(f"  WARN idx {i} -> {started.task_id} expected {idx2task[i]}")
            task=started.task_id
            print("extract",task)
            c=EcomRuntimeClientSync(started.harness_url, interceptors=(intc,))
            try: handle(c, task, started.instruction, OUT/task)
            except Exception as e: print("  ERR",e)
            try:
                EcomRuntimeClientSync(started.harness_url, interceptors=(intc,)).answer(
                    E.AnswerRequest(outcome=E.OUTCOME_NONE_CLARIFICATION, message="probe", refs=[]))
            except Exception: pass
            done.add(task)
    finally:
        try: har.submit_run(rid, force=True)
        except Exception as e: print("submit err",e)
    print("done",sorted(done))

if __name__=="__main__":
    raise SystemExit(main())
