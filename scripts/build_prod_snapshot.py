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

Store SHAPE fidelity (see artifacts/prod_explore/prod_truth/sample_store_*.json
and the byte-faithful scraped worlds under
artifacts/ws_snapshots/prod_run1/.../proc/locations/): a real PROD store
record is SMALL (~4.5 KB, 35-36 inventory entries) with top-level fields
``id, name, address_line_1, postal_code, city, country_code, is_open,
lat, lon, inventory``. The deep-extract ``catalogue.db`` is a DEV-schema
projection: its ``store_inventory`` is DENSE (2.4k-10k stocked SKUs per
store, all with positive availability) and its ``stores`` table has no
address fields. So this materializer can faithfully reproduce the store
FIELD SHAPE but NOT PROD's real ~35-SKU stocking subset — that subset is
not recoverable from the db. Two consequences are handled here:

  * Field shape: address_line_1/postal_code/country_code are emitted
    (synthesized deterministically from city; the db lacks them, so they
    are placeholders, not real PROD values — they exist only so the
    record shape and key set match PROD under the 16 KiB read cap).
  * Size: ``--max-inventory N`` caps the embedded inventory to a
    prod-realistic bound. Default is UNLIMITED so an existing count
    oracle is never silently corrupted (dropping a task-relevant SKU
    would flip "available" to absent=0 and change the count). When a
    store exceeds the prod-realistic band a WARNING is printed.

For oracle-faithful COUNT test beds, prefer scripts/scrape_prod_worlds.py:
it writes the exact ~35-entry store the agent actually read on PROD.

Usage:
    scripts/build_prod_snapshot.py --from artifacts/ws_snapshots/t16_real2 \
        --out artifacts/ws_snapshots/t16_prod \
        [--truth artifacts/prod_explore/prod_truth] \
        [--max-inventory 35]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

_DEFAULT_TRUTH = Path("artifacts/prod_explore/prod_truth")

# Real PROD stores carry 35-36 inventory entries (~4.5 KB). A store record
# materially larger than this is unfaithful AND breaks under the 16 KiB
# read cap (a 200 KB store truncates to invalid JSON; see test_build_prod
# _snapshot.test_store_truncates_cleanly_under_read_cap). Used only to warn.
_PROD_REALISTIC_MAX_INVENTORY = 60

# country_code is a fixed 2-letter ISO code in every scraped PROD store.
# The deep-extract db has no per-store country; PROD's worlds are all
# Austria/CZ/SI but the db cannot tell us which, so we default to "AT"
# (the modal scraped value) as a clearly-synthetic placeholder.
_DEFAULT_COUNTRY_CODE = "AT"


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _store_record(row: dict, inventory: list[dict]) -> dict:
    """PROD-shaped store JSON with embedded inventory.

    Field set + order match the byte-faithful scraped PROD stores and
    artifacts/prod_explore/prod_truth/sample_store_*.json:
        id, name, address_line_1, postal_code, city, country_code,
        is_open, lat, lon, inventory

    The deep-extract db has no address_line_1 / postal_code /
    country_code columns, so those are SYNTHESIZED deterministically from
    the city. They are placeholders (NOT real PROD values); they exist so
    the record's key set and approximate size match PROD. lat/lon come
    from the db when present (PROD always has them; if the db row lacks
    them they are omitted rather than faked, since they carry geo meaning
    the address placeholder does not)."""
    city = row.get("city") or "Unknown"
    store_id = row["store_id"]
    rec: dict = {
        "id": store_id,
        "name": row.get("store_name"),
        # placeholder address fields — see docstring (db has none)
        "address_line_1": f"{city} branch (address not in deep-extract db)",
        "postal_code": "0000",
        "city": city,
        "country_code": row.get("country_code") or _DEFAULT_COUNTRY_CODE,
        "is_open": bool(row.get("is_open")),
    }
    if row.get("latitude") is not None:
        rec["lat"] = row["latitude"]
    if row.get("longitude") is not None:
        rec["lon"] = row["longitude"]
    rec["inventory"] = inventory
    return rec


def _cap_inventory(inventory: list[dict], max_inventory: Optional[int]) -> list[dict]:
    """Bound an embedded inventory to ``max_inventory`` entries.

    Selection is DETERMINISTIC (sorted by SKU, the same order PROD uses
    for inventory exports per /docs/availability-checks.md) so repeated
    builds are stable. NOTE: capping a DENSE db store can drop a SKU that
    a count task names and that IS available, lowering the count — that is
    why the default is no cap. Use the cap only for shape/size/truncation
    testing, not for count-oracle beds."""
    if max_inventory is None or len(inventory) <= max_inventory:
        return inventory
    return sorted(inventory, key=lambda e: e.get("sku") or "")[:max_inventory]


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
    """PROD-shaped catalogue record.

    Field set + order match artifacts/prod_explore/prod_truth/
    sample_catalog_*.json:
        id, sku, name, brand, category_id, kind_id, family_id,
        price_cents, fulfillment_type, return_policy, properties

    fulfillment_type and return_policy are small integer enums on PROD but
    the deep-extract db has NO such columns. They are emitted with a
    deterministic placeholder (1) so the record key set matches PROD;
    callers MUST NOT treat the value as authoritative — it is synthetic.
    If the db ever grows these columns the real value is used."""
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
        # PROD-shape enum fields; db lacks them → synthetic placeholder 1
        "fulfillment_type": int(r["fulfillment_type"]) if r.get("fulfillment_type") not in (None, "") else 1,
        "return_policy": int(r["return_policy"]) if r.get("return_policy") not in (None, "") else 1,
        "properties": props or {},
    }
    return rec


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build(
    *,
    from_snapshot: Path,
    out_dir: Path,
    truth_dir: Optional[Path] = None,
    max_inventory: Optional[int] = None,
) -> Path:
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
        bloated: list[tuple[str, int]] = []
        for r in conn.execute("SELECT * FROM stores"):
            d = dict(r)
            city = (d.get("city") or "Unknown")
            full_inv = inv_by_store.get(d["store_id"], [])
            inv = _cap_inventory(full_inv, max_inventory)
            rec = _store_record(d, inv)
            _write_json(out_ws / "proc" / "locations" / city / f"{d['store_id']}.json", rec)
            n_stores += 1
            if len(inv) > _PROD_REALISTIC_MAX_INVENTORY:
                bloated.append((d["store_id"], len(inv)))

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
          f"{n_stores} stores, {n_cat} catalog records"
          + (f" (inventory capped at {max_inventory}/store)" if max_inventory else ""))
    if bloated:
        import sys as _sys
        worst = max(n for _, n in bloated)
        print(
            f"# WARNING: {len(bloated)} store(s) carry >{_PROD_REALISTIC_MAX_INVENTORY} "
            f"inventory entries (worst {worst}); real PROD stores carry ~35-36 "
            f"(~4.5 KB). The deep-extract db is a DENSE DEV projection and cannot "
            f"reproduce PROD's real stocking subset. Such a store EXCEEDS the 16 KiB "
            f"read cap and truncates to invalid JSON. For SIZE/shape testing pass "
            f"--max-inventory 35; for oracle-faithful count beds use "
            f"scripts/scrape_prod_worlds.py.",
            file=_sys.stderr,
        )
    return out_dir


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="from_snapshot", type=Path, required=True)
    p.add_argument("--out", dest="out_dir", type=Path, required=True)
    p.add_argument("--truth", dest="truth_dir", type=Path, default=None)
    p.add_argument(
        "--max-inventory", dest="max_inventory", type=int, default=None,
        help="cap embedded inventory entries per store to a PROD-realistic "
             "bound (PROD stores carry ~35-36). Default: no cap (preserves "
             "count-oracle correctness — see module docstring). Pass 35 for "
             "shape/size/truncation testing.")
    a = p.parse_args()
    build(from_snapshot=a.from_snapshot, out_dir=a.out_dir,
          truth_dir=a.truth_dir, max_inventory=a.max_inventory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
