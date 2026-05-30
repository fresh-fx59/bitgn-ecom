#!/usr/bin/env python3
"""Materialize a PROD-faithful workspace snapshot from a *_real* snapshot.

The local test harness drifts from ECOM1-PROD in two ways that break the
count/availability and product-resolution families (see
docs/superpowers/specs/2026-05-30-prod-faithful-harness-design.md):

  1. Data drift — inventory lives in the SQL `store_inventory` table, so
     with SQL off the agent has nothing to read. On PROD inventory is
     EMBEDDED in the store record at /proc/locations/<City>/<store>.json.
  2. Documentation drift — snapshot /AGENTS.MD + /docs are the dev
     ("SQL projections") versions, not PROD's file-based ones.

This script reads a prod-schema `catalogue.db` and writes a standalone
PROD-shaped workspace: stores with embedded inventory, catalog records,
and the real PROD AGENTS.MD + docs (from a captured truth dir). No
catalogue.db is emitted, so SQL is genuinely unavailable — run the agent
against the output with BITGN_LOCAL_SQL_UNAVAILABLE=1.

Crucially, `available_today` is NOT written into inventory entries: PROD
does not expose it, and the agent must compute max(on_hand-reserved,0)
per /docs/availability-checks.md — which is exactly where count tasks err.

Usage:
    scripts/build_prod_snapshot.py --from artifacts/ws_snapshots/t16_real2 \
        --out artifacts/ws_snapshots/t16_prod \
        [--truth artifacts/prod_explore/prod_truth]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

_DEFAULT_TRUTH = Path("artifacts/prod_explore/prod_truth")


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _store_record(row: dict, inventory: list[dict]) -> dict:
    """PROD-shaped store JSON with embedded inventory. Address fields are
    not in the deep-extract db (and irrelevant to the broken families),
    so they are omitted; the inventory array is the fidelity-critical part."""
    rec = {
        "id": row["store_id"],
        "name": row.get("store_name"),
        "city": row.get("city"),
        "is_open": bool(row.get("is_open")),
    }
    if row.get("latitude") is not None:
        rec["lat"] = row["latitude"]
    if row.get("longitude") is not None:
        rec["lon"] = row["longitude"]
    rec["inventory"] = inventory
    return rec


def _inventory_entry(r: dict) -> dict:
    """One PROD inventory entry — {sku,on_hand,reserved[,incoming]}.
    NO available_today: PROD makes the agent compute it."""
    e = {
        "sku": r["product_sku"],
        "on_hand": int(r.get("on_hand_quantity") or 0),
        "reserved": int(r.get("reserved_quantity") or 0),
    }
    inc_q = int(r.get("incoming_quantity") or 0)
    if inc_q > 0:
        e["incoming"] = [{
            "quantity": inc_q,
            "arrival_in_days": int(r.get("next_arrival_in_days") or 0),
        }]
    return e


def _catalog_record(r: dict, rid: int) -> dict:
    props = r.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (ValueError, TypeError):
            props = {}
    rec = {
        "id": rid,
        "sku": r["product_sku"],
        "name": r.get("product_name"),
        "brand": r.get("brand"),
        "category_id": r.get("product_category_id"),
        "kind_id": r.get("product_kind_id"),
        "family_id": r.get("product_family_id"),
        "price_cents": int(r["price_cents"]) if r.get("price_cents") not in (None, "") else None,
        "properties": props or {},
    }
    return rec


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build(*, from_snapshot: Path, out_dir: Path, truth_dir: Optional[Path] = None) -> Path:
    from_snapshot = Path(from_snapshot)
    out_dir = Path(out_dir)
    truth_dir = Path(truth_dir) if truth_dir else _DEFAULT_TRUTH

    src_ws = from_snapshot / "run_0" / "workspace"
    db_paths = list(src_ws.glob("*.db"))
    if not db_paths:
        raise FileNotFoundError(f"no catalogue db under {src_ws}")
    db = db_paths[0]

    out_ws = out_dir / "run_0" / "workspace"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_ws.mkdir(parents=True)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # ---- stores + embedded inventory → /proc/locations/<City>/<id>.json
        inv_by_store: dict[str, list[dict]] = {}
        if "store_inventory" in _table_names(conn):
            for r in conn.execute("SELECT * FROM store_inventory"):
                d = dict(r)
                inv_by_store.setdefault(d["store_id"], []).append(_inventory_entry(d))
        n_stores = 0
        for r in conn.execute("SELECT * FROM stores"):
            d = dict(r)
            city = (d.get("city") or "Unknown")
            rec = _store_record(d, inv_by_store.get(d["store_id"], []))
            _write_json(out_ws / "proc" / "locations" / city / f"{d['store_id']}.json", rec)
            n_stores += 1

        # ---- catalog → /proc/catalog/<Brand>/<sku>.json
        n_cat = 0
        for i, r in enumerate(conn.execute("SELECT * FROM product_variants"), start=1):
            d = dict(r)
            brand = (d.get("brand") or "Unknown")
            _write_json(out_ws / "proc" / "catalog" / brand / f"{d['product_sku']}.json",
                        _catalog_record(d, i))
            n_cat += 1
    finally:
        conn.close()

    # ---- PROD docs + AGENTS.MD (remove documentation drift)
    agents = truth_dir / "AGENTS.MD"
    if agents.exists():
        (out_ws / "AGENTS.MD").write_text(agents.read_text(encoding="utf-8"), encoding="utf-8")
    docs_src = truth_dir / "docs"
    if docs_src.exists():
        for f in docs_src.iterdir():
            if not f.is_file():
                continue
            # captured names flatten /docs/payments/3ds.md → payments__3ds.md
            rel = f.name.replace("__", "/")
            dest = out_ws / "docs" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    # ---- carry metadata over
    src_meta = from_snapshot / "run_0" / "metadata.json"
    if src_meta.exists():
        meta = json.loads(src_meta.read_text(encoding="utf-8"))
        meta.setdefault("source", str(from_snapshot))
        meta["prod_faithful"] = True
        (out_dir / "run_0" / "metadata.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8")

    print(f"# built prod-faithful snapshot at {out_dir}: "
          f"{n_stores} stores, {n_cat} catalog records")
    return out_dir


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="from_snapshot", type=Path, required=True)
    p.add_argument("--out", dest="out_dir", type=Path, required=True)
    p.add_argument("--truth", dest="truth_dir", type=Path, default=None)
    a = p.parse_args()
    build(from_snapshot=a.from_snapshot, out_dir=a.out_dir, truth_dir=a.truth_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
