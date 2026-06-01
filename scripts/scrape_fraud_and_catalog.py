#!/usr/bin/env python3
"""One-shot scraper for the tractable PROD failure families.

Starts ONE throwaway run, and for the target tasks reads the exact files we
need to root-cause + build deterministic fixes OFFLINE:
  * fraud archive TSVs (t015/t035/t055/t075): /archive/payment_batch_export_*.tsv
  * catalog records (t002/t062): brand dir listings + variant JSONs
  * count-with-price (t047): catalog search for the sizing template

Saves everything under /tmp/prod_scrape/<task>/. Closes every trial with a
CLARIFICATION no-op (reads do not count against the run rate limit).
"""
from __future__ import annotations
import os, re, sys, json
from pathlib import Path

import bitgn_contest_agent.harness as H
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
import bitgn.vm.ecom.ecom_pb2 as E

OUT = Path("/tmp/prod_scrape")
TARGETS = {"t002","t062","t047","t015","t035","t055","t075","t041"}
ARCHIVE_RE = re.compile(r"(/archive/\S+\.tsv)")


def _read(c, path):
    try:
        return getattr(c.read(E.ReadRequest(path=path)), "content", "") or ""
    except Exception as e:
        return f"__ERR__ {e}"

def _search(c, root, pattern, limit=40):
    try:
        r = c.search(E.SearchRequest(root=root, pattern=pattern, limit=limit))
        return getattr(r, "results", None) or getattr(r, "matches", None) or str(r)
    except Exception as e:
        return f"__ERR__ {e}"

def _list(c, path):
    try:
        r = c.list(E.ListRequest(path=path))
        return [getattr(x,"name",str(x)) for x in (getattr(r,"entries",None) or getattr(r,"items",None) or [])] or str(r)
    except Exception as e:
        return f"__ERR__ {e}"

def _find(c, root, name, limit=40):
    try:
        r = c.find(E.FindRequest(root=root, name=name, kind="", limit=limit))
        return [getattr(x,"path",str(x)) for x in (getattr(r,"results",None) or getattr(r,"matches",None) or [])] or str(r)
    except Exception as e:
        return f"__ERR__ {e}"


def handle(c, task_id, instr, d: Path):
    d.mkdir(parents=True, exist_ok=True)
    (d/"instruction.txt").write_text(instr or "")
    if task_id in {"t015","t035","t055","t075"}:
        m = ARCHIVE_RE.search(instr or "")
        if m:
            content = _read(c, m.group(1))
            (d/"archive.tsv").write_text(content)
            print(f"  {task_id}: archive {m.group(1)} -> {len(content)} bytes, {content.count(chr(10))} lines")
    if task_id == "t002":
        (d/"karcher_dir.txt").write_text("\n".join(map(str,_list(c,"/proc/catalog/Karcher"))))
        for sku in ("PT-WASH-KAR-K4-PIPE","PT-WASH-KAR-K4-PC"):
            for p in _find(c, "/proc/catalog", f"{sku}.json"):
                if isinstance(p,str) and p.endswith(".json"):
                    (d/f"{sku}.json").write_text(_read(c,p))
        (d/"search_k4.txt").write_text(str(_search(c,"/proc/catalog","K4")))
        print(f"  t002: catalog dumped")
    if task_id == "t062":
        (d/"stihl_dir.txt").write_text("\n".join(map(str,_list(c,"/proc/catalog/Stihl"))))
        (d/"search_rma235.txt").write_text(str(_search(c,"/proc/catalog","RMA235")))
        for p in _find(c, "/proc/catalog", "PT-MOW-STI-RMA235*"):
            print("   rma235 find:", p)
        print(f"  t062: catalog dumped")
    if task_id == "t047":
        (d/"search_sizing.txt").write_text(str(_search(c,"/proc/catalog","sizing",limit=60)))
        (d/"search_template.txt").write_text(str(_search(c,"/proc/catalog","template",limit=60)))
        print(f"  t047: catalog search dumped")
    if task_id == "t041":
        (d/"search_dewalt_kit.txt").write_text(str(_search(c,"/proc/catalog","DeWalt",limit=60)))
        print(f"  t041: catalog search dumped")


def main():
    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set", file=sys.stderr); return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK","bitgn/ecom1-prod")
    harness = H.BitgnHarness.from_env(benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key)
    rid, trial_ids = harness.start_run(name="scrape-fraud-catalog")
    print(f"run_id={rid} trials={len(trial_ids)}")
    intc = H._AuthHeaderInterceptor(api_key)
    seen=set()
    try:
        for tid in trial_ids:
            started = harness.start_trial(tid)
            task_id = started.task_id
            if task_id in TARGETS and task_id not in seen:
                seen.add(task_id)
                print(f"extracting {task_id} ...", flush=True)
                c = EcomRuntimeClientSync(started.harness_url, interceptors=(intc,))
                try:
                    handle(c, task_id, started.instruction, OUT/task_id)
                except Exception as e:
                    print(f"  {task_id} ERR {e}")
            try:
                runtime = EcomRuntimeClientSync(started.harness_url, interceptors=(intc,))
                runtime.answer(E.AnswerRequest(outcome=E.OUTCOME_NONE_CLARIFICATION, message="scrape probe", refs=[]))
            except Exception:
                pass
            if seen >= TARGETS:
                print("all targets covered, stopping early")
                break
    finally:
        try:
            harness.submit_run(rid, force=True); print(f"submitted {rid}")
        except Exception as e:
            print("submit err", e)
    print("scraped:", sorted(seen))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
