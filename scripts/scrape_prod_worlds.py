#!/usr/bin/env python3
"""Rebuild faithful per-task local worlds from a TASK-TAGGED raw dump.

PROD has no working /bin/sql, so deep_extract_trial (which dumps SQL tables)
cannot build faithful PROD test beds. But every filesystem RPC IS captured in
the per-process raw dump, and (since the task-id tagging in ecom_tracing) each
record now carries its `task`. This scraper slices the dump by task and writes
the exact files each task READ (byte-faithful) into a standalone workspace —
no SQL needed. The task instruction + the agent's own answer come from the
per-task trace. The result is a viable local test bed for families the
prod-faithful materializer can't synthesize (notably the raw-SKU-list count
family), so a count lever's CONVENTION can be validated against the real
store inventory the agent saw.

Usage:
  scripts/scrape_prod_worlds.py \
      --dump artifacts/raw_dumps/bench_<ts>/ecom_responses.<pid>.jsonl \
      --traces-dir logs/<run_ts> \
      --targets t005 t025 t045 t065 \
      --out artifacts/ws_snapshots --tag scraped
  # or --count-only to auto-pick tasks whose text matches the count shape
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from pathlib import Path


def _load_dump(dump_glob: str) -> dict[str, list[dict]]:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for f in glob.glob(dump_glob):
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("task")
            if t:
                by_task[t].append(r)
    return by_task


def _trace_task_text(traces_dir: str, task_id: str) -> tuple[str, str]:
    """(instruction, agent_final_answer) from the per-task trace."""
    instr = answer = ""
    for cand in (f"{task_id}__run0.jsonl", f"{task_id}_run0.jsonl"):
        p = Path(traces_dir) / cand
        if not p.exists():
            continue
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "task" and not instr:
                instr = r.get("task_text") or ""
            if r.get("kind") == "step":
                fn = (r.get("next_step") or {}).get("function") or {}
                if fn.get("tool") == "report_completion":
                    answer = fn.get("message") or ""
        break
    return instr, answer


def _rebuild(task_id: str, recs: list[dict], instr: str, answer: str, out_root: Path) -> int:
    ws = out_root / f"{task_id}_scraped" / "run_0" / "workspace"
    if ws.parent.parent.exists():
        import shutil
        shutil.rmtree(ws.parent.parent)
    ws.mkdir(parents=True)
    n = 0
    for r in recs:
        if r.get("op") != "read" or not r.get("ok"):
            continue
        path = (r.get("request") or {}).get("path") or ""
        content = (r.get("response") or {}).get("content")
        if not path or content is None:
            continue
        dest = ws / path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        n += 1
    meta = {
        "instruction": instr,
        "agent_answer": answer,          # what the agent reported (NOT an oracle)
        "expected_answer": None,         # fill after eval / by hand
        "source": "scraped from prod task-tagged dump",
        "prod_faithful": True,
    }
    (ws.parent / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return n


_COUNT_SIGNALS = ("how many of these skus", "how many of these products",
                  "how many catalogue products", "do you have")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True, help="glob to ecom_responses.*.jsonl")
    ap.add_argument("--traces-dir", required=True)
    ap.add_argument("--targets", nargs="*", default=[])
    ap.add_argument("--count-only", action="store_true",
                    help="auto-select tasks whose instruction looks like a count")
    ap.add_argument("--out", type=Path, default=Path("artifacts/ws_snapshots"))
    ap.add_argument("--tag", default="scraped")
    a = ap.parse_args()

    by_task = _load_dump(a.dump)
    if not by_task:
        raise SystemExit(f"no task-tagged records in {a.dump} (run must use the "
                         "task-tagged build of ecom_tracing)")
    targets = a.targets or sorted(by_task)
    built = []
    for t in targets:
        if t not in by_task:
            print(f"[skip] {t}: no records")
            continue
        instr, answer = _trace_task_text(a.traces_dir, t)
        if a.count_only and not any(s in (instr or "").lower() for s in _COUNT_SIGNALS):
            continue
        n = _rebuild(t, by_task[t], instr, answer, a.out)
        built.append((t, n, answer[:40]))
        print(f"[built] {t}: {n} files | agent_answer={answer[:40]!r}")
    print(f"\n# scraped {len(built)} task world(s) → {a.out}/<task>_scraped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
