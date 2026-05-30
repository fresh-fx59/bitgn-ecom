"""Tests for the prod-faithful snapshot materializer.

Verifies that build_prod_snapshot converts a prod-schema catalogue.db into
a standalone PROD-shaped workspace: stores under /proc/locations/<City>/
with EMBEDDED inventory (on_hand/reserved, no available_today — the agent
must compute max(on_hand-reserved,0)), catalog under /proc/catalog/<Brand>/,
the PROD AGENTS.MD + docs, and no catalogue.db (SQL genuinely unavailable).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
spec = importlib.util.spec_from_file_location(
    "build_prod_snapshot", _SCRIPTS / "build_prod_snapshot.py")
bps = importlib.util.module_from_spec(spec)
sys.modules["build_prod_snapshot"] = bps
spec.loader.exec_module(bps)


def _make_source_snapshot(root: Path) -> Path:
    """A minimal *_real-shaped source: run_0/{workspace/catalogue.db, metadata.json}."""
    ws = root / "run_0" / "workspace"
    ws.mkdir(parents=True)
    (root / "run_0" / "metadata.json").write_text(json.dumps({
        "instruction": "how many SKUs have >=1 available at Vienna store?",
        "expected_outcome": "OUTCOME_OK",
        "context_date": "2026-05-30T00:00:00Z",
    }))
    db = ws / "catalogue.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE stores (store_id TEXT, record_path TEXT, "
                 "store_name TEXT, city TEXT, is_open INT, latitude REAL, longitude REAL)")
    conn.execute("INSERT INTO stores VALUES ('store_vienna_x','/proc/stores/store_vienna_x.json',"
                 "'PowerTools Vienna X','Vienna',1,48.2,16.3)")
    conn.execute("CREATE TABLE store_inventory (store_id TEXT, product_sku TEXT, "
                 "on_hand_quantity INT, reserved_quantity INT, available_today_quantity INT, "
                 "incoming_quantity INT, next_arrival_in_days INT)")
    # one in-stock (on_hand 5, reserved 2 -> avail 3), one with incoming only
    conn.execute("INSERT INTO store_inventory VALUES ('store_vienna_x','PT-A',5,2,3,0,0)")
    conn.execute("INSERT INTO store_inventory VALUES ('store_vienna_x','PT-B',0,0,0,4,3)")
    conn.execute("CREATE TABLE product_variants (product_sku TEXT, record_path TEXT, "
                 "brand TEXT, product_name TEXT, price_cents INT, price_currency TEXT, "
                 "product_category_id TEXT, product_kind_id TEXT, product_family_id TEXT, properties TEXT)")
    conn.execute("INSERT INTO product_variants VALUES ('PT-A','/proc/catalog/x/PT-A.json',"
                 "'Makita','Makita Drill','9990','EUR','cat-x','kind-y','fam-z','{\"voltage_v\": 18}')")
    conn.commit(); conn.close()
    return root


def _make_truth(root: Path) -> Path:
    t = root / "truth"
    (t / "docs").mkdir(parents=True)
    (t / "AGENTS.MD").write_text("# ECOM1 Production Workspace\nStores under /proc/locations.\n")
    (t / "docs" / "availability-checks.md").write_text(
        "Same-day availability is `max(on_hand - reserved, 0)`.\n")
    return t


@pytest.fixture
def built(tmp_path):
    src = _make_source_snapshot(tmp_path / "src")
    truth = _make_truth(tmp_path)
    out = tmp_path / "out_prod"
    bps.build(from_snapshot=src, out_dir=out, truth_dir=truth)
    return out / "run_0" / "workspace"


def test_store_record_at_locations_city_path(built):
    f = built / "proc" / "locations" / "Vienna" / "store_vienna_x.json"
    assert f.exists(), "store must materialize at /proc/locations/<City>/<id>.json"
    d = json.loads(f.read_text())
    assert d["id"] == "store_vienna_x"
    assert d["is_open"] is True
    assert isinstance(d.get("inventory"), list) and len(d["inventory"]) == 2


def test_inventory_embedded_without_available_today(built):
    d = json.loads((built / "proc" / "locations" / "Vienna" / "store_vienna_x.json").read_text())
    by = {e["sku"]: e for e in d["inventory"]}
    assert by["PT-A"]["on_hand"] == 5 and by["PT-A"]["reserved"] == 2
    # available_today must NOT be present — the agent computes it
    assert "available_today" not in by["PT-A"]
    assert "available_today_quantity" not in by["PT-A"]
    # incoming only when incoming_quantity > 0
    assert "incoming" not in by["PT-A"]
    assert by["PT-B"]["incoming"] == [{"quantity": 4, "arrival_in_days": 3}]


def test_catalog_record_prod_shape(built):
    f = built / "proc" / "catalog" / "Makita" / "PT-A.json"
    assert f.exists()
    d = json.loads(f.read_text())
    assert d["sku"] == "PT-A" and d["brand"] == "Makita"
    assert d["category_id"] == "cat-x" and d["kind_id"] == "kind-y" and d["family_id"] == "fam-z"
    assert d["price_cents"] == 9990
    assert d["properties"] == {"voltage_v": 18}  # parsed, not a string


def test_prod_docs_and_no_db(built):
    assert (built / "AGENTS.MD").exists()
    assert (built / "docs" / "availability-checks.md").exists()
    # SQL must be genuinely unavailable: no catalogue.db in the prod workspace
    assert not list(built.glob("*.db"))


def test_metadata_carried_over(built):
    meta = json.loads((built.parent / "metadata.json").read_text())
    assert meta["instruction"].startswith("how many")
    assert meta["expected_outcome"] == "OUTCOME_OK"
