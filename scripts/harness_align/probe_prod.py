#!/usr/bin/env python3
"""Burn one ECOM trial to capture wire-level response shapes from PROD.

The goal is a side-by-side comparison fixture: the same RPCs against
the real EcomRuntime vs. against `LocalEcomClient`, so we can verify
that the local harness duck-types correctly and replay tasks
deterministically.

What this script does (one trial, ~30s, costs 1 leaderboard slot):

  1. start_run + start_trial against bitgn/ecom1-dev
  2. Issue every ECOM RPC at least once:
       tree (level 1, level 2, level 0),
       list (root and a known sub),
       read (/AGENTS.MD with and without line slicing),
       find (kind=all / files / dirs),
       search (regex with limit), stat (file + dir),
       exec /bin/sql (".schema" and a SELECT against products),
       exec /bin/id, exec /bin/date, exec /bin/checkout
       (post-freeze replacements for the retired context() RPC)
  3. Serialize each response with MessageToDict and save under
       artifacts/harness_align/prod/<op>__<descr>.json
  4. submit a deliberate OUTCOME_NONE_CLARIFICATION answer with a
     short note so the grader doesn't count this as a real attempt
  5. end_trial + submit_run

Outputs:
  artifacts/harness_align/prod/manifest.json     — index + metadata
  artifacts/harness_align/prod/<op>__<descr>.json — one per probe
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    StartRunRequest,
    StartTrialRequest,
    SubmitRunRequest,
)
from bitgn.vm.ecom import ecom_pb2
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
from connectrpc.interceptor import MetadataInterceptorSync
from google.protobuf.json_format import MessageToDict


OUT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "harness_align" / "prod"


class _Auth(MetadataInterceptorSync):
    def __init__(self, key: str) -> None:
        self._key = key

    def on_start_sync(self, ctx) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._key}"


def _save(name: str, msg: Any) -> str:
    """Save the proto response as JSON and return the relative filename
    (str — not Path, so the manifest stays json-serializable)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    payload = MessageToDict(msg, preserving_proto_field_name=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return f"{name}.json"


def _try(label: str, fn):
    """Run a probe, capture errors to the manifest so a server-side
    error on one probe doesn't kill the rest of the run."""
    try:
        return {"label": label, "ok": True, "result": fn()}
    except Exception as exc:
        return {"label": label, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    api_key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")
    interceptors = (_Auth(api_key),)
    harness = HarnessServiceClientSync(base, interceptors=interceptors)

    # 1. start a leaderboard run, take the first trial
    print(f"starting leaderboard run on {bench} …")
    run = harness.start_run(StartRunRequest(
        benchmark_id=bench, name=f"harness-align-{int(time.time())}",
        api_key=api_key,
    ))
    trial_id = run.trial_ids[0]
    print(f"run_id={run.run_id} trial_id={trial_id} (of {len(run.trial_ids)})")
    started = harness.start_trial(StartTrialRequest(trial_id=trial_id))
    vm = EcomRuntimeClientSync(started.harness_url, interceptors=interceptors)
    print(f"task_id={started.task_id} harness_url={started.harness_url}")
    print(f"instruction: {started.instruction[:120]}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "task.json").write_text(json.dumps({
        "task_id": started.task_id,
        "trial_id": started.trial_id,
        "harness_url": started.harness_url,
        "instruction": started.instruction,
        "benchmark_id": started.benchmark_id,
    }, indent=2), encoding="utf-8")

    manifest: list[dict] = []

    # 2a. tree at root with several level caps
    for level in (0, 1, 2):
        manifest.append(_try(f"tree_root_level{level}", lambda l=level: _save(
            f"tree_root_level{l}",
            vm.tree(ecom_pb2.TreeRequest(root="/", level=l)),
        )))
    manifest.append(_try("tree_proc_level1", lambda: _save(
        "tree_proc_level1",
        vm.tree(ecom_pb2.TreeRequest(root="/proc", level=1)),
    )))

    # 2c. list at root and at a known sub (we'll target /docs which the
    #     rulebook says exists; if it doesn't we'll capture the error)
    manifest.append(_try("list_root", lambda: _save(
        "list_root", vm.list(ecom_pb2.ListRequest(path="/")),
    )))
    manifest.append(_try("list_docs", lambda: _save(
        "list_docs", vm.list(ecom_pb2.ListRequest(path="/docs")),
    )))

    # 2d. read AGENTS.MD whole + sliced
    manifest.append(_try("read_agents_md", lambda: _save(
        "read_agents_md", vm.read(ecom_pb2.ReadRequest(path="/AGENTS.MD")),
    )))
    manifest.append(_try("read_agents_md_sliced", lambda: _save(
        "read_agents_md_sliced",
        vm.read(ecom_pb2.ReadRequest(
            path="/AGENTS.MD", start_line=1, end_line=5, number=True,
        )),
    )))

    # 2e. find — try a name that should exist + a name that probably won't
    for kind_name, kind_val in (
        ("all", ecom_pb2.NodeKind.NODE_KIND_UNSPECIFIED),
        ("files", ecom_pb2.NodeKind.NODE_KIND_FILE),
        ("dirs", ecom_pb2.NodeKind.NODE_KIND_DIR),
    ):
        manifest.append(_try(f"find_{kind_name}_AGENTS", lambda k=kind_val, n=kind_name: _save(
            f"find_{n}_AGENTS",
            vm.find(ecom_pb2.FindRequest(
                root="/", name="AGENTS", kind=k, limit=5,
            )),
        )))

    # 2f. search — pattern almost certainly present in /AGENTS.MD
    manifest.append(_try("search_TODO", lambda: _save(
        "search_TODO",
        vm.search(ecom_pb2.SearchRequest(root="/", pattern="catalog", limit=5)),
    )))

    # 2g. stat — file + dir
    manifest.append(_try("stat_agents_md", lambda: _save(
        "stat_agents_md", vm.stat(ecom_pb2.StatRequest(path="/AGENTS.MD")),
    )))
    manifest.append(_try("stat_root", lambda: _save(
        "stat_root", vm.stat(ecom_pb2.StatRequest(path="/")),
    )))

    # 2h. exec /bin/sql — schema and a tiny SELECT
    manifest.append(_try("exec_sql_schema", lambda: _save(
        "exec_sql_schema",
        vm.exec(ecom_pb2.ExecRequest(
            path="/bin/sql", args=[], stdin=".schema",
        )),
    )))
    manifest.append(_try("exec_sql_count_products", lambda: _save(
        "exec_sql_count_products",
        vm.exec(ecom_pb2.ExecRequest(
            path="/bin/sql", args=[],
            stdin="SELECT count(*) AS n FROM products;",
        )),
    )))
    manifest.append(_try("exec_unknown_bin", lambda: _save(
        "exec_unknown_bin",
        vm.exec(ecom_pb2.ExecRequest(
            path="/bin/does-not-exist", args=[], stdin="",
        )),
    )))

    # 2h-bis. exec /bin/id, /bin/date, /bin/checkout — post-freeze
    # replacements for the retired context() RPC. /bin/checkout is
    # probed with no args to capture its `--help`-shaped exit.
    manifest.append(_try("exec_id", lambda: _save(
        "exec_id",
        vm.exec(ecom_pb2.ExecRequest(path="/bin/id", args=[], stdin="")),
    )))
    manifest.append(_try("exec_date", lambda: _save(
        "exec_date",
        vm.exec(ecom_pb2.ExecRequest(path="/bin/date", args=[], stdin="")),
    )))
    manifest.append(_try("exec_checkout", lambda: _save(
        "exec_checkout",
        vm.exec(ecom_pb2.ExecRequest(path="/bin/checkout", args=[], stdin="")),
    )))

    # 2i. capture proc layout (we hit this namespace in failures)
    manifest.append(_try("list_proc", lambda: _save(
        "list_proc", vm.list(ecom_pb2.ListRequest(path="/proc")),
    )))
    manifest.append(_try("tree_proc_stores", lambda: _save(
        "tree_proc_stores",
        vm.tree(ecom_pb2.TreeRequest(root="/proc/stores", level=2)),
    )))
    manifest.append(_try("tree_proc_catalog", lambda: _save(
        "tree_proc_catalog",
        vm.tree(ecom_pb2.TreeRequest(root="/proc/catalog", level=2)),
    )))

    # 3. submit an honest "I was probing" answer so the trial closes
    print(f"submitting probe answer for {started.task_id} …")
    try:
        vm.answer(ecom_pb2.AnswerRequest(
            message="probe-only run; no real answer attempted",
            outcome=ecom_pb2.Outcome.OUTCOME_NONE_CLARIFICATION,
            refs=[],
        ))
    except Exception as exc:
        print(f"  (answer call raised: {exc!r})")

    end = harness.end_trial(EndTrialRequest(trial_id=trial_id))
    print(f"end_trial: score={end.score} details={list(end.score_detail)[:3]}")
    harness.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
    print("submitted run.")

    (OUT_DIR / "manifest.json").write_text(json.dumps({
        "task_id": started.task_id,
        "trial_id": started.trial_id,
        "run_id": run.run_id,
        "probes": manifest,
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {len(manifest)} probe captures under {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
