#!/usr/bin/env python3
"""Probe the entire bitgn/ecom1-dev read-only surface across one
leaderboard run and persist every response shape verbatim.

Use this to (a) learn the harness's structural invariants — the
file tree, the doc set, the /bin inventory, the SQL schema, the
content_type taxonomy — so the LocalEcomClient mock can mirror them
exactly, and (b) measure what randomises between trials so the
local fixtures can stay generic. Run twice in succession and diff
the outputs to separate "structural truth" from "per-trial seed".

Each trial in the run is opened via StartTrial, probed exhaustively
with READ-ONLY ops (tree/list/read/find/search/stat/exec), then
closed with `answer(OUTCOME_NONE_CLARIFICATION, "scanner probe")` so
the trial reaches a terminal state and the dashboard does not show
an orphan running row (per AGENTS.md Process safety).

Outputs:

    artifacts/scans/run_<run_id>_<ts>/
        manifest.json           # run-level info + per-trial summary
        trials/
            <task_id>/
                meta.json       # task_id, trial_id, instruction, harness_url
                tree.json       # tree(/, level=0) — full inventory
                list_<dir>.json # one per top-level dir
                read_<sanitised-path>.json
                exec_<bin>.json
                sql_schema.json # exec(/bin/sql, '.schema')
                sql_tables.json # exec(/bin/sql, '.tables')

Environment:
    BITGN_API_KEY     — required (leaderboard run requires auth)
    BITGN_BASE_URL    — default https://api.bitgn.com
    BITGN_BENCHMARK   — default bitgn/ecom1-dev
    SCAN_OUT          — override output dir (default artifacts/scans/...)
    SCAN_DEPTH        — sample reads to N per entity dir (default 3)

Usage:
    scripts/scan_ecom1.py
    scripts/scan_ecom1.py --read-every       # read every file, not just samples
    scripts/scan_ecom1.py --max-trials 5     # cap for fast iteration

Exit code 0 on success (even if individual probes failed — failures
are captured in each trial's meta.json).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    StartRunRequest,
    StartTrialRequest,
    SubmitRunRequest,
)
from bitgn.vm.ecom import ecom_pb2
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
from connectrpc.interceptor import MetadataInterceptorSync
from google.protobuf.json_format import MessageToDict


# ── auth + helpers ──────────────────────────────────────────────────


class _Auth(MetadataInterceptorSync):
    def __init__(self, key: str) -> None:
        self._key = key

    def on_start_sync(self, ctx) -> None:
        ctx.request_headers()["authorization"] = f"Bearer {self._key}"


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise(path: str) -> str:
    """Filesystem-safe slug for the workspace path so probe output
    files do not collide."""
    return _SAFE_RE.sub("_", path.strip("/")) or "root"


def _to_dict(msg: Any) -> dict:
    try:
        return MessageToDict(msg, preserving_proto_field_name=True)
    except Exception:
        return {"__repr__": repr(msg)}


def _save(out: Path, name: str, payload: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{name}.json"
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _try(label: str, fn: Callable[[], Any]) -> dict:
    t0 = time.monotonic()
    try:
        result = fn()
        return {
            "label": label, "ok": True,
            "wall_ms": int((time.monotonic() - t0) * 1000),
            "result": _to_dict(result),
        }
    except Exception as exc:
        return {
            "label": label, "ok": False,
            "wall_ms": int((time.monotonic() - t0) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


# ── per-trial probe battery ─────────────────────────────────────────


@dataclass
class TrialProbeConfig:
    read_every: bool
    sample_per_dir: int


def _walk_paths(tree_root: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a tree response into [(absolute_path, kind), ...].

    kind ∈ {"NODE_KIND_FILE", "NODE_KIND_DIR", "NODE_KIND_UNSPECIFIED"}.
    """
    out: list[tuple[str, str]] = []
    name = tree_root.get("name", "")
    kind = tree_root.get("kind", "")
    children = tree_root.get("children") or []
    cur = "/" if name in ("", "/") else (
        prefix.rstrip("/") + "/" + name if prefix else "/" + name
    )
    out.append((cur, kind))
    for ch in children:
        out.extend(_walk_paths(ch, cur))
    return out


def _classify_entities(paths: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Group file paths by their /proc/<entity>/ namespace (and a
    /docs bucket + /bin bucket + a misc bucket) so the probe can
    sample a small number per group."""
    groups: dict[str, list[str]] = {}
    for p, kind in paths:
        if kind != "NODE_KIND_FILE":
            continue
        if p.startswith("/docs/"):
            groups.setdefault("docs", []).append(p)
        elif p.startswith("/bin/"):
            groups.setdefault("bin", []).append(p)
        elif p.startswith("/proc/"):
            parts = p.split("/")
            if len(parts) >= 3:
                groups.setdefault(f"proc:{parts[2]}", []).append(p)
            else:
                groups.setdefault("proc:misc", []).append(p)
        else:
            groups.setdefault("misc", []).append(p)
    for k in groups:
        groups[k].sort()
    return groups


def probe_trial(
    *,
    vm: EcomRuntimeClientSync,
    out: Path,
    task_id: str,
    trial_id: str,
    instruction: str,
    harness_url: str,
    config: TrialProbeConfig,
) -> dict:
    """Run the full probe battery against one trial's VM."""
    summary: dict[str, Any] = {
        "task_id": task_id,
        "trial_id": trial_id,
        "instruction": instruction,
        "harness_url": harness_url,
        "probes": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )

    # 1. Full tree (level=0 means unlimited recursion per proto)
    tree_probe = _try("tree_full", lambda: vm.tree(
        ecom_pb2.TreeRequest(root="/", level=0),
    ))
    _save(out, "tree", tree_probe)
    summary["probes"].append({"label": "tree_full", "ok": tree_probe["ok"]})
    if not tree_probe["ok"]:
        # No tree → no point in further inventory probing. Still hit
        # the universal RPCs so we have some shape data.
        for label, req in [
            ("list_root", ecom_pb2.ListRequest(path="/")),
            ("exec_id", ecom_pb2.ExecRequest(path="/bin/id")),
            ("exec_date", ecom_pb2.ExecRequest(path="/bin/date")),
        ]:
            kind = label.split("_", 1)[0]
            probe = _try(label, lambda r=req, k=kind: getattr(vm, k)(r))
            _save(out, label, probe)
            summary["probes"].append({"label": label, "ok": probe["ok"]})
        return summary

    paths = _walk_paths(tree_probe["result"].get("root") or {})
    summary["path_count"] = len(paths)

    # 2. List every directory (cheap, structural inventory)
    dirs = [p for p, k in paths if k == "NODE_KIND_DIR"]
    for d in dirs:
        label = f"list_{_sanitise(d)}"
        probe = _try(label, lambda dp=d: vm.list(ecom_pb2.ListRequest(path=dp)))
        _save(out, label, probe)
        summary["probes"].append({"label": label, "ok": probe["ok"], "dir": d})

    # 3. Read every doc + every /bin/* (these are the policy / tool
    #    inventory the agent cites). Then sample entity records per
    #    /proc/<entity>/ namespace.
    groups = _classify_entities(paths)
    summary["groups"] = {k: len(v) for k, v in groups.items()}
    targets: list[str] = []
    targets.extend(groups.get("docs", []))
    targets.extend(groups.get("bin", []))
    if config.read_every:
        for k, v in groups.items():
            if k not in ("docs", "bin"):
                targets.extend(v)
    else:
        # Always include the README per entity group (the agent reads
        # those for taxonomy hints) plus N random samples.
        for k, v in groups.items():
            if k in ("docs", "bin"):
                continue
            readmes = [p for p in v if p.endswith("README.md")]
            non_readmes = [p for p in v if not p.endswith("README.md")]
            targets.extend(readmes)
            targets.extend(non_readmes[: config.sample_per_dir])

    seen: set[str] = set()
    for tp in targets:
        if tp in seen:
            continue
        seen.add(tp)
        label = f"read_{_sanitise(tp)}"
        probe = _try(label, lambda pp=tp: vm.read(ecom_pb2.ReadRequest(path=pp)))
        _save(out, label, probe)
        summary["probes"].append({"label": label, "ok": probe["ok"], "path": tp})

    # 4. Exec the standard /bin inventory we expect on PROD post-freeze.
    #    /bin/sql is probed separately with .schema and .tables. Others
    #    are exec'd with no args so we capture their "default" output —
    #    safe for read-only bins, may error for state-mutating bins
    #    (errors captured in the probe record, not raised).
    for label, args, stdin in [
        ("exec_id", [], ""),
        ("exec_date", [], ""),
        ("exec_sql_schema", [], ".schema"),
        ("exec_sql_tables", [], ".tables"),
    ]:
        path = "/bin/" + label.split("_", 1)[1].split("_", 1)[0]
        if label.startswith("exec_sql"):
            path = "/bin/sql"
        probe = _try(label, lambda p=path, a=args, s=stdin: vm.exec(
            ecom_pb2.ExecRequest(path=p, args=a, stdin=s),
        ))
        _save(out, label, probe)
        summary["probes"].append({"label": label, "ok": probe["ok"], "path": path})

    # Any other /bin/* discovered in the tree (e.g. /bin/checkout,
    # /bin/payments, /bin/discount) gets probed with no args too.
    for bp in groups.get("bin", []):
        if bp in ("/bin/id", "/bin/date", "/bin/sql"):
            continue
        if bp.endswith("README.md"):
            continue
        label = f"exec_{_sanitise(bp)}"
        probe = _try(label, lambda p=bp: vm.exec(
            ecom_pb2.ExecRequest(path=p, args=[], stdin=""),
        ))
        _save(out, label, probe)
        summary["probes"].append({"label": label, "ok": probe["ok"], "path": bp})

    # 5. stat samples — one dir, one file
    if dirs:
        d = dirs[0]
        probe = _try("stat_dir", lambda dp=d: vm.stat(ecom_pb2.StatRequest(path=dp)))
        _save(out, "stat_dir", probe)
        summary["probes"].append({"label": "stat_dir", "ok": probe["ok"], "path": d})
    files = [p for p, k in paths if k == "NODE_KIND_FILE" and not p.startswith("/bin/")]
    if files:
        f0 = files[0]
        probe = _try("stat_file", lambda fp=f0: vm.stat(
            ecom_pb2.StatRequest(path=fp),
        ))
        _save(out, "stat_file", probe)
        summary["probes"].append({"label": "stat_file", "ok": probe["ok"], "path": f0})

    # 6. search smoke — a generic term that should hit most workspaces
    probe = _try("search_policy", lambda: vm.search(
        ecom_pb2.SearchRequest(root="/", pattern="policy", limit=5),
    ))
    _save(out, "search_policy", probe)
    summary["probes"].append({"label": "search_policy", "ok": probe["ok"]})

    summary["ended_at"] = datetime.now(timezone.utc).isoformat()
    # Overwrite meta.json with the populated summary
    (out / "meta.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return summary


# ── orchestration ───────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scan_ecom1", description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--max-trials", type=int, default=None,
                   help="Cap trials per run (default: all)")
    p.add_argument("--read-every", action="store_true",
                   help="Read every file, not a sample (slower, more dump volume)")
    p.add_argument("--sample-per-dir", type=int, default=3,
                   help="Per-entity-group sample size when --read-every is off")
    p.add_argument("--label", default=None,
                   help="Suffix appended to the output dir (e.g. run1, run2)")
    args = p.parse_args(argv)

    api_key = os.environ["BITGN_API_KEY"]
    base = os.environ.get("BITGN_BASE_URL", "https://api.bitgn.com")
    bench = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")
    interceptors = (_Auth(api_key),)
    harness = HarnessServiceClientSync(base, interceptors=interceptors)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root_env = os.environ.get("SCAN_OUT")
    if out_root_env:
        out_root = Path(out_root_env)
    else:
        suffix = f"_{args.label}" if args.label else ""
        out_root = Path("artifacts/scans") / f"scan_{ts}{suffix}"

    # Run name matches the dashboard convention pinned by the user
    # (2026-05-15): owner handle + benchmark tag + commit short SHA +
    # purpose. Scanner runs are tagged "scan-<ts>" so they sort
    # next to but distinct from the leaderboard "bench" runs.
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        sha = "unknown"
    run_name = f"@ai_engineer_helper DEV-ECOM1 {sha} scan-{ts}"
    print(f"[scanner] benchmark={bench} → {out_root}", flush=True)
    print(f"[scanner] run name: {run_name}", flush=True)
    run = harness.start_run(StartRunRequest(
        benchmark_id=bench, name=run_name, api_key=api_key,
    ))
    trial_ids = list(run.trial_ids)
    if args.max_trials:
        trial_ids = trial_ids[: args.max_trials]
    print(f"[scanner] run_id={run.run_id}  trials={len(trial_ids)}", flush=True)

    manifest: dict[str, Any] = {
        "scanner_version": "0.1",
        "benchmark": bench,
        "run_id": run.run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "trials": [],
        "config": {
            "read_every": args.read_every,
            "sample_per_dir": args.sample_per_dir,
            "max_trials": args.max_trials,
            "label": args.label,
        },
    }

    config = TrialProbeConfig(
        read_every=args.read_every,
        sample_per_dir=args.sample_per_dir,
    )

    for i, trial_id in enumerate(trial_ids, 1):
        print(f"[scanner] trial {i}/{len(trial_ids)} {trial_id} …", flush=True)
        try:
            started = harness.start_trial(StartTrialRequest(trial_id=trial_id))
        except Exception as exc:
            print(f"  start_trial failed: {exc}", flush=True)
            manifest["trials"].append({
                "trial_id": trial_id, "error": str(exc),
            })
            continue
        task_id = started.task_id
        vm = EcomRuntimeClientSync(started.harness_url, interceptors=interceptors)
        trial_out = out_root / "trials" / task_id

        try:
            summary = probe_trial(
                vm=vm, out=trial_out,
                task_id=task_id, trial_id=trial_id,
                instruction=started.instruction,
                harness_url=started.harness_url,
                config=config,
            )
            manifest["trials"].append({
                "task_id": task_id, "trial_id": trial_id,
                "path_count": summary.get("path_count"),
                "groups": summary.get("groups"),
                "n_probes": len(summary.get("probes") or []),
                "n_failed_probes": sum(
                    1 for x in summary.get("probes", []) if not x.get("ok")
                ),
            })
        finally:
            # Close the trial — per AGENTS.md, every started trial
            # must reach a terminal state.
            try:
                vm.answer(ecom_pb2.AnswerRequest(
                    message="scanner probe — no answer attempted",
                    outcome=ecom_pb2.Outcome.OUTCOME_NONE_CLARIFICATION,
                    refs=[],
                ))
            except Exception as exc:
                print(f"  answer() failed: {exc}", flush=True)
            try:
                harness.end_trial(EndTrialRequest(trial_id=trial_id))
            except Exception as exc:
                print(f"  end_trial failed: {exc}", flush=True)

    try:
        harness.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
    except Exception as exc:
        print(f"[scanner] submit_run failed: {exc}", flush=True)

    manifest["ended_at"] = datetime.now(timezone.utc).isoformat()
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    print(f"[scanner] done. manifest: {out_root}/manifest.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
