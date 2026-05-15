#!/usr/bin/env python3
"""Diff LocalEcomClient responses against PROD captures.

Replays every (op, request) pair from a scanner trial against a
freshly-rebuilt local workspace (built from the same trial's reads)
and reports per-op:

    OK    — JSON shape matches PROD verbatim
    SHAPE — same top-level keys, value mismatch (expected for content)
    KEYS  — different top-level keys (DRIFT — must fix)
    ERR   — local raised but PROD succeeded (or vice versa)

The script doesn't run a full bench — it replays one trial's
read-only probes for cheap, repeatable wire alignment checks.

Usage:
    scripts/verify_local_mirror.py --trial-dir artifacts/scans/<scan>/trials/<task_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from google.protobuf.json_format import MessageToDict

from bitgn_contest_agent.local.ecom_client import LocalEcomClient


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise(p: str) -> str:
    return _SAFE_RE.sub("_", p.strip("/")) or "root"


def _build_workspace(trial_dir: Path, out: Path) -> tuple[str, str]:
    """Reconstruct a usable LocalEcomClient workspace from the
    trial's read probes — same shape as rebuild_ws_from_raw.py but
    sourcing from the scanner's per-probe JSON files."""
    out.mkdir(parents=True, exist_ok=True)
    files = 0
    actor = "anonymous"
    roles = "GUEST"
    context_date = "2026-05-15T12:00:00Z"
    for probe_file in trial_dir.glob("read_*.json"):
        d = json.loads(probe_file.read_text())
        if not d.get("ok"):
            continue
        r = d["result"]
        p = r.get("path") or ""
        content = r.get("content", "")
        if not p:
            continue
        rel = p.lstrip("/")
        full = (out / rel).resolve()
        if not str(full).startswith(str(out.resolve())):
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        files += 1
    # Materialise empty dirs from tree so list() against any dir works
    tree = json.loads((trial_dir / "tree.json").read_text()).get("result", {}).get("root") or {}

    def walk(e, prefix=""):
        name = e.get("name", "")
        kind = e.get("kind", "")
        cur = "/" if name in ("", "/") else (prefix.rstrip("/") + "/" + name if prefix else "/" + name)
        if kind == "NODE_KIND_DIR":
            rel = cur.lstrip("/")
            (out / rel).mkdir(parents=True, exist_ok=True)
        elif kind == "NODE_KIND_FILE":
            rel = cur.lstrip("/")
            f = out / rel
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("", encoding="utf-8")
        for c in e.get("children") or []:
            walk(c, cur)
    walk(tree)
    # Try to extract actor/roles from the trial's exec_id capture
    id_probe = trial_dir / "exec_id.json"
    if id_probe.exists():
        d = json.loads(id_probe.read_text())
        if d.get("ok"):
            stdout = (d["result"] or {}).get("stdout", "")
            m_user = re.search(r"^user:\s*(.+)$", stdout, re.MULTILINE)
            m_roles = re.search(r"^roles:\s*(.+)$", stdout, re.MULTILINE)
            if m_user:
                actor = m_user.group(1).strip()
            if m_roles:
                roles = m_roles.group(1).strip()
    # Same for /bin/date
    date_probe = trial_dir / "exec_date.json"
    if date_probe.exists():
        d = json.loads(date_probe.read_text())
        if d.get("ok"):
            stdout = (d["result"] or {}).get("stdout", "").strip()
            if stdout:
                context_date = stdout
    return actor, roles


def _req(**kw) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _shape(d: Any) -> str:
    if d is None:
        return "None"
    if isinstance(d, dict):
        keys = sorted(d.keys())
        return "{" + ",".join(keys) + "}"
    if isinstance(d, list):
        return "[" + (_shape(d[0]) if d else "") + "...]"
    return type(d).__name__


def _classify(probe_op: str, prod: dict | None, local: dict | None) -> str:
    if prod is None and local is None:
        return "OK"
    if prod is None or local is None:
        return "ERR"
    if prod == local:
        return "OK"
    if set(prod.keys()) != set(local.keys()):
        return "KEYS"
    return "SHAPE"


def _replay_probe(client: LocalEcomClient, probe: dict, trial_dir: Path) -> tuple[str, dict, dict]:
    """Re-issue the probe against the local client and return
    (label, prod_response, local_response)."""
    from bitgn.vm.ecom import ecom_pb2

    label = probe.get("label", "?")
    fp = trial_dir / f"{label}.json"
    if not fp.exists():
        return label, {}, {}
    prod_record = json.loads(fp.read_text())
    if not prod_record.get("ok"):
        return label, prod_record, {}
    prod_resp = prod_record.get("result") or {}

    # Reconstruct the request from the label + probe path
    path = probe.get("path") or probe.get("dir")
    try:
        if label.startswith("tree"):
            resp = client.tree(_req(root="/", level=0))
        elif label.startswith("list_"):
            resp = client.list(_req(path=path or "/"))
        elif label.startswith("read_"):
            resp = client.read(_req(path=path, number=False, start_line=0, end_line=0))
        elif label == "exec_id":
            resp = client.exec(_req(path="/bin/id", args=[], stdin=""))
        elif label == "exec_date":
            resp = client.exec(_req(path="/bin/date", args=[], stdin=""))
        elif label == "exec_sql_schema":
            resp = client.exec(_req(path="/bin/sql", args=[], stdin=".schema"))
        elif label == "exec_sql_tables":
            resp = client.exec(_req(path="/bin/sql", args=[], stdin=".tables"))
        elif label.startswith("exec_bin_"):
            bin_path = (probe.get("path") or "/bin/" + label[len("exec_bin_"):]).split("/")[-1]
            resp = client.exec(_req(path=f"/bin/{bin_path}", args=[], stdin=""))
        elif label == "stat_dir" or label == "stat_file":
            resp = client.stat(_req(path=path))
        elif label == "search_policy":
            resp = client.search(_req(root="/", pattern="policy", limit=5))
        else:
            return label, prod_resp, {"__skip": label}
    except Exception as exc:
        return label, prod_resp, {"__exc": f"{type(exc).__name__}: {exc}"}

    local_resp = MessageToDict(resp, preserving_proto_field_name=True)
    return label, prod_resp, local_resp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verify_local_mirror")
    p.add_argument("--trial-dir", type=Path, required=True,
                   help="artifacts/scans/<scan>/trials/<task_id>")
    p.add_argument("--workspace-out", type=Path, default=None,
                   help="Where to materialise the trial workspace "
                        "(default: tmp under the trial dir)")
    p.add_argument("--show-mismatches", type=int, default=10,
                   help="Print up to N mismatched probes in detail")
    p.add_argument("--ignore-shape-mismatch-on", action="append",
                   default=["content", "stdout", "matches", "entries", "children", "instruction"],
                   help="Don't print 'SHAPE' mismatches whose only diff is in these keys "
                        "(content/stdout/etc. naturally differ on reconstructed workspaces)")
    args = p.parse_args(argv)

    trial_dir: Path = args.trial_dir
    if not (trial_dir / "meta.json").exists():
        raise SystemExit(f"meta.json missing under {trial_dir}")
    meta = json.loads((trial_dir / "meta.json").read_text())

    ws_out = args.workspace_out or (trial_dir / "_mirror_workspace")
    actor, roles = _build_workspace(trial_dir, ws_out)
    print(f"# workspace: {ws_out}")
    print(f"# actor: {actor!r}  roles: {roles!r}")

    client = LocalEcomClient(
        workspace_root=ws_out, actor_id=actor, roles=roles,
        context_date=meta.get("started_at"),
    )

    results = Counter()
    detailed: list[tuple[str, str, dict, dict]] = []
    for probe in meta.get("probes", []):
        label, prod, local = _replay_probe(client, probe, trial_dir)
        verdict = _classify(probe.get("label"), prod.get("result") if isinstance(prod, dict) and "result" in prod else prod, local)
        results[verdict] += 1
        if verdict in ("KEYS", "ERR"):
            detailed.append((verdict, label, prod, local))
        elif verdict == "SHAPE":
            # Demote SHAPE mismatches whose ONLY divergence is on
            # values inside an ignored key.
            prod_v = prod.get("result") if isinstance(prod, dict) and "result" in prod else prod
            keys_with_diff = [k for k in (prod_v or {}).keys() if (prod_v or {}).get(k) != (local or {}).get(k)]
            if all(k in args.ignore_shape_mismatch_on for k in keys_with_diff):
                results["SHAPE"] -= 1
                results["OK_value_drift"] += 1
            else:
                detailed.append((verdict, label, prod, local))

    print()
    print("# verdict tally:")
    for k, v in results.most_common():
        print(f"  {k:<20} {v}")
    if detailed:
        print()
        print(f"# detail (up to {args.show_mismatches}):")
        for verdict, label, prod, local in detailed[: args.show_mismatches]:
            print(f"  --- [{verdict}] {label} ---")
            print(f"    PROD : {json.dumps(prod, sort_keys=True)[:400]}")
            print(f"    LOCAL: {json.dumps(local, sort_keys=True)[:400]}")
    return 0 if results.get("KEYS", 0) == 0 and results.get("ERR", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
