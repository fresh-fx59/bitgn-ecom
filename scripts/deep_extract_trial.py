#!/usr/bin/env python3
"""Deep-extract live ECOM trials into faithful local snapshots.

Unlike rebuild_ws_from_raw (which only sees what the agent queried) and
scan_ecom1 (structure only), this dumps the COMPLETE SQL schema + every
row of every base table from a LIVE trial, plus the small workspace
files, then builds a PROD-schema `catalogue.db` so the LocalEcomClient
can replay the exact task offline with byte-faithful SQL behaviour.

One leaderboard run (counts against the 10/30min rate limit). Each
trial is opened, deep-extracted if its task_id is in --targets (or all
if --all-targets), then closed with answer(NONE_CLARIFICATION) so the
dashboard shows no orphan. submit_run finalises.

The product catalogue is typically STATIC across trials; pass
--catalogue-from <task_id> to dump the big catalogue tables only once
and symlink/copy into the other snapshots.

Usage:
  scripts/deep_extract_trial.py --targets t16 t45 t40 t48 t53 \
      --out artifacts/ws_snapshots --tag real0529
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import bitgn_contest_agent.harness as H
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
import bitgn.vm.ecom.ecom_pb2 as E
from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import StartTrialRequest, SubmitRunRequest


def _exec(client, path, stdin="", args=None):
    resp = client.exec(E.ExecRequest(path=path, stdin=stdin, args=list(args or [])))
    return {
        "stdout": getattr(resp, "stdout", ""),
        "exit_code": getattr(resp, "exit_code", None),
        "stderr": getattr(resp, "stderr", ""),
    }


def _read(client, path):
    try:
        resp = client.read(E.ReadRequest(path=path))
        return getattr(resp, "content", "") or ""
    except Exception as e:
        return f"__READ_ERROR__ {e}"


def _sql(client, query):
    r = _exec(client, "/bin/sql", stdin=query)
    return r["stdout"], r.get("stderr", "")


def _list_tables(client):
    out, _ = _sql(client, "SELECT name FROM sqlite_master WHERE type='table';")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # drop header 'name' if present
    return [l for l in lines if l and l.lower() != "name"]


def _table_info(client, table):
    """Return [(name, type)] via pragma_table_info — gives real column
    affinities so numeric comparisons (qty >= 3) work in the local db."""
    out, err = _sql(
        client,
        f"SELECT name, type FROM pragma_table_info('{table}');",
    )
    if err and "no such" in err.lower():
        return None
    info = []
    for i, line in enumerate(out.splitlines()):
        if i == 0:  # header 'name,type'
            continue
        parts = _csv_row(line)
        if len(parts) >= 2:
            info.append((parts[0], parts[1] or "TEXT"))
    return info or None


def _create_table_sql(table, info):
    cols = ", ".join(f'"{n}" {t or "TEXT"}' for n, t in info)
    return f'CREATE TABLE "{table}" ({cols});'


def _row_count(client, table):
    out, err = _sql(client, f'SELECT COUNT(*) FROM "{table}";')
    if err:
        return None
    for i, line in enumerate(out.splitlines()):
        if i == 0:
            continue
        try:
            return int(line.strip())
        except ValueError:
            return None
    return None


def _dump_table(client, table, info):
    """Paginate SELECT * to capture all rows. Page size adapts down on
    truncation so wide tables are captured completely."""
    colnames = [n for n, _ in info]
    total = _row_count(client, table)
    rows = []
    offset = 0
    page = 40
    guard = 0
    while (total is None or offset < total) and guard < 100_000:
        guard += 1
        q = f'SELECT * FROM "{table}" LIMIT {page} OFFSET {offset};'
        out, err = _sql(client, q)
        if err and "no such table" in err.lower():
            return None
        lines = out.splitlines()
        truncated = any(l.startswith("[TRUNCATED") for l in lines)
        body = [l for l in lines[1:] if not l.startswith("[TRUNCATED")]
        if truncated and page > 5:
            page = max(5, page // 2)
            continue  # retry same offset with smaller page
        if not body:
            break
        for b in body:
            vals = _csv_row(b)
            if len(vals) == len(colnames):
                rows.append(dict(zip(colnames, vals)))
            else:
                rows.append({"__raw__": b})
        offset += len(body)
        if total is None and len(body) < page:
            break
    return rows


def _csv_row(line):
    # naive CSV split; catalogue text rarely contains quoted commas, but
    # handle simple quoted fields
    import csv
    import io
    return next(csv.reader(io.StringIO(line)))


SMALL_FILES = [
    "/AGENTS.MD",
]
README_DIRS = ["stores", "employees", "payments", "baskets", "customers", "returns"]


def deep_extract(client, out_dir: Path, task_id, instruction):
    ws = out_dir / "run_0" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    meta = {"task_id": task_id, "instruction": instruction}

    # identity + clock
    meta["bin_id"] = _exec(client, "/bin/id").get("stdout", "").strip()
    meta["bin_date"] = _exec(client, "/bin/date").get("stdout", "").strip()

    # tree
    try:
        tr = client.tree(E.TreeRequest(root="/", level=4))
        (out_dir / "tree.json").write_text(
            json.dumps({"root": _node(tr.root)}, indent=1)
        )
    except Exception as e:
        (out_dir / "tree.json").write_text(json.dumps({"error": str(e)}))

    # small files
    for p in SMALL_FILES:
        c = _read(client, p)
        _write_ws(ws, p, c)
    for d in README_DIRS:
        c = _read(client, f"/proc/{d}/README.md")
        if not c.startswith("__READ_ERROR__"):
            _write_ws(ws, f"/proc/{d}/README.md", c)
    # docs
    try:
        dt = client.tree(E.TreeRequest(root="/docs", level=4))
        for path in _walk_files(dt.root, "/docs"):
            _write_ws(ws, path, _read(client, path))
    except Exception:
        pass
    # uploads (OCR receipts)
    try:
        ut = client.tree(E.TreeRequest(root="/uploads", level=3))
        for path in _walk_files(ut.root, "/uploads"):
            _write_ws(ws, path, _read(client, path))
    except Exception:
        pass

    # SQL: per-table schema (via pragma) + full row dumps → catalogue.db
    tables = _list_tables(client)
    meta["tables"] = {}
    sql_dir = out_dir / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    db_path = ws / "catalogue.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    schema_lines = []
    for t in tables:
        info = _table_info(client, t)
        if not info:
            continue
        create = _create_table_sql(t, info)
        schema_lines.append(create)
        try:
            conn.execute(create)
        except Exception as e:
            meta.setdefault("schema_errors", {})[t] = str(e)
            continue
        rows = _dump_table(client, t, info)
        if rows is None:
            continue
        # persist raw rows so data survives even if insert hiccups
        (sql_dir / f"{t}.json").write_text(json.dumps(rows))
        meta["tables"][t] = len(rows)
        _insert_rows(conn, t, [n for n, _ in info], rows)
    (out_dir / "sql_schema.sql").write_text("\n".join(schema_lines))
    conn.commit()
    conn.close()

    (out_dir / "run_0" / "metadata.json").write_text(
        json.dumps(meta, indent=1)
    )
    return meta


def _node(n):
    d = {"name": n.name, "kind": int(n.kind)}
    ch = list(getattr(n, "children", []))
    if ch:
        d["children"] = [_node(c) for c in ch]
    return d


def _walk_files(node, prefix):
    out = []
    for c in getattr(node, "children", []):
        path = f"{prefix}/{c.name}".replace("//", "/")
        if int(c.kind) == 1:  # NODE_KIND_FILE
            out.append(path)
        elif int(c.kind) == 2:  # NODE_KIND_DIR
            out.extend(_walk_files(c, path))
    return out


def _write_ws(ws: Path, path: str, content: str):
    if content.startswith("__READ_ERROR__"):
        return
    rel = path.lstrip("/")
    dst = ws / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content)


def _insert_rows(conn, table, cols, rows):
    if not rows:
        return
    # discover actual table columns
    try:
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        tcols = [r[1] for r in info]
    except Exception:
        tcols = cols or []
    if not tcols:
        return
    for row in rows:
        if "__raw__" in row:
            continue
        vals = [row.get(c) for c in tcols]
        ph = ",".join("?" * len(tcols))
        try:
            conn.execute(
                f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in tcols)}) VALUES ({ph})',
                vals,
            )
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", nargs="*", default=[])
    ap.add_argument("--all-targets", action="store_true")
    ap.add_argument("--out", default="artifacts/ws_snapshots")
    ap.add_argument("--tag", default="real")
    args = ap.parse_args()

    api_key = os.environ.get("BITGN_API_KEY")
    if not api_key:
        print("BITGN_API_KEY not set", file=sys.stderr)
        return 2
    base_url = os.environ.get("BITGN_BASE_URL") or "https://api.bitgn.com"
    benchmark = os.environ.get("BITGN_BENCHMARK", "bitgn/ecom1-dev")

    harness = H.BitgnHarness.from_env(
        benchmark=benchmark, bitgn_base_url=base_url, bitgn_api_key=api_key
    )
    rid, trial_ids = harness.start_run(name="deep-extract")
    print(f"run_id={rid} trials={len(trial_ids)}")
    Path(".last_run_id").write_text(rid)
    intc = H._AuthHeaderInterceptor(api_key)

    targets = set(args.targets)
    extracted = {}
    try:
        for tid in trial_ids:
            started = harness.start_trial(tid)
            task_id = started.task_id
            want = args.all_targets or task_id in targets
            if want:
                print(f"  extracting {task_id} ...", flush=True)
                client = EcomRuntimeClientSync(started.harness_url, interceptors=(intc,))
                out_dir = Path(args.out) / f"{task_id}_{args.tag}"
                try:
                    meta = deep_extract(client, out_dir, task_id, started.instruction)
                    extracted[task_id] = meta.get("tables", {})
                    print(f"    {task_id}: tables={meta.get('tables')}")
                except Exception as e:
                    print(f"    {task_id}: EXTRACT ERROR {e}")
            # close trial
            try:
                runtime = EcomRuntimeClientSync(started.harness_url, interceptors=(intc,))
                runtime.answer(E.AnswerRequest(
                    outcome=E.OUTCOME_NONE_CLARIFICATION,
                    message="extractor probe",
                    refs=[],
                ))
            except Exception as e:
                print(f"    close {task_id} err: {e}")
    finally:
        try:
            harness.submit_run(rid, force=True)
            print(f"submitted run {rid}")
        except Exception as e:
            print(f"submit err: {e}")
    print("extracted:", list(extracted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
