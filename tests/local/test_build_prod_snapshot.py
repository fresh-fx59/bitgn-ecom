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


# ---- PROD store SHAPE fidelity ---------------------------------------------
# Reference: artifacts/prod_explore/prod_truth/sample_store_*.json and the
# byte-faithful scraped stores under artifacts/ws_snapshots/prod_run1/.
# A real PROD store record's top-level key set + ORDER is exactly:
_PROD_STORE_KEYS = [
    "id", "name", "address_line_1", "postal_code", "city",
    "country_code", "is_open", "lat", "lon", "inventory",
]


def test_store_top_level_keys_match_prod_shape(built):
    """Materialized store key set + order must equal the real PROD record."""
    d = json.loads(
        (built / "proc" / "locations" / "Vienna" / "store_vienna_x.json").read_text())
    assert list(d.keys()) == _PROD_STORE_KEYS


def test_store_has_address_fields_present_on_prod(built):
    """PROD stores carry address_line_1 / postal_code / country_code; the
    db lacks them so they are synthesized, but the KEYS must exist so the
    record shape matches PROD under the 16 KiB read cap."""
    d = json.loads(
        (built / "proc" / "locations" / "Vienna" / "store_vienna_x.json").read_text())
    assert isinstance(d["address_line_1"], str) and d["address_line_1"]
    assert isinstance(d["postal_code"], str)
    assert isinstance(d["country_code"], str) and len(d["country_code"]) == 2


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


# ---- PROD catalogue SHAPE fidelity -----------------------------------------
# Reference: artifacts/prod_explore/prod_truth/sample_catalog_*.json
_PROD_CATALOG_KEYS = [
    "id", "sku", "name", "brand", "category_id", "kind_id", "family_id",
    "price_cents", "fulfillment_type", "return_policy", "properties",
]


def test_catalog_keys_match_prod_shape(built):
    """Catalogue record key set + order must equal the real PROD record,
    including fulfillment_type / return_policy (db lacks them → synthesized)."""
    d = json.loads((built / "proc" / "catalog" / "Makita" / "PT-A.json").read_text())
    assert list(d.keys()) == _PROD_CATALOG_KEYS
    # synthesized enum placeholders are small ints (PROD shape)
    assert isinstance(d["fulfillment_type"], int)
    assert isinstance(d["return_policy"], int)


def test_prod_docs_and_no_db(built):
    assert (built / "AGENTS.MD").exists()
    assert (built / "docs" / "availability-checks.md").exists()
    # SQL must be genuinely unavailable: no catalogue.db in the prod workspace
    assert not list(built.glob("*.db"))


def test_metadata_carried_over(built):
    meta = json.loads((built.parent / "metadata.json").read_text())
    assert meta["instruction"].startswith("how many")
    assert meta["expected_outcome"] == "OUTCOME_OK"


# ---- inventory SIZE bound (gap #1: store bloat) ----------------------------

def _make_dense_source(root: Path, n: int = 500) -> Path:
    """A source whose store stocks ``n`` SKUs — mimics the DENSE deep-extract
    db (2.4k-10k stocked SKUs/store), which is the real bloat source."""
    ws = root / "run_0" / "workspace"
    ws.mkdir(parents=True)
    (root / "run_0" / "metadata.json").write_text(json.dumps({"instruction": "x"}))
    db = ws / "catalogue.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE stores (store_id TEXT, store_name TEXT, city TEXT, "
                 "is_open INT, latitude REAL, longitude REAL)")
    conn.execute("INSERT INTO stores VALUES ('store_vienna_x','PowerTools Vienna X',"
                 "'Vienna',1,48.2,16.3)")
    conn.execute("CREATE TABLE store_inventory (store_id TEXT, product_sku TEXT, "
                 "on_hand_quantity INT, reserved_quantity INT, available_today_quantity INT, "
                 "incoming_quantity INT, next_arrival_in_days INT)")
    for i in range(n):
        conn.execute("INSERT INTO store_inventory VALUES "
                     "('store_vienna_x',?,?,?,?,?,?)",
                     (f"PT-{i:05d}", 5, 1, 4, 0, 0))
    conn.execute("CREATE TABLE product_variants (product_sku TEXT, brand TEXT, "
                 "product_name TEXT, price_cents INT, product_category_id TEXT, "
                 "product_kind_id TEXT, product_family_id TEXT, properties TEXT)")
    for i in range(n):
        conn.execute("INSERT INTO product_variants VALUES (?,?,?,?,?,?,?,?)",
                     (f"PT-{i:05d}", "Makita", f"Tool {i}", 1000,
                      "cat-x", "kind-y", "fam-z", "{}"))
    conn.commit(); conn.close()
    return root


def test_max_inventory_caps_store_to_prod_realistic_size(tmp_path):
    """gap #1: a dense (500-SKU) source must cap to ~35 entries and the
    record must stay small enough to read in full under the 16 KiB cap."""
    src = _make_dense_source(tmp_path / "src")
    truth = _make_truth(tmp_path)
    out = tmp_path / "out"
    bps.build(from_snapshot=src, out_dir=out, truth_dir=truth, max_inventory=35)
    f = out / "run_0" / "workspace" / "proc" / "locations" / "Vienna" / "store_vienna_x.json"
    raw = f.read_bytes()
    d = json.loads(raw.decode("utf-8"))
    assert len(d["inventory"]) == 35
    # PROD store records are ~4.5 KB; a 35-entry record stays well under cap
    assert len(raw) < 16 * 1024


def test_uncapped_dense_store_bloats(tmp_path):
    """Document gap #1: without a cap the dense db dumps every SKU, so the
    record blows past the 16 KiB read cap (the unfaithful default)."""
    src = _make_dense_source(tmp_path / "src", n=500)
    truth = _make_truth(tmp_path)
    out = tmp_path / "out"
    bps.build(from_snapshot=src, out_dir=out, truth_dir=truth)  # no cap
    f = out / "run_0" / "workspace" / "proc" / "locations" / "Vienna" / "store_vienna_x.json"
    d = json.loads(f.read_bytes().decode("utf-8"))
    assert len(d["inventory"]) == 500


def test_capped_store_truncates_cleanly_under_read_cap(tmp_path):
    """A capped (prod-sized) store is read IN FULL under the 16 KiB cap —
    no truncation, valid JSON — exactly like a real PROD store; an
    uncapped dense store truncates to INVALID JSON (behavior-changing)."""
    src = _make_dense_source(tmp_path / "src", n=500)
    truth = _make_truth(tmp_path)
    cap = 16 * 1024

    out_capped = tmp_path / "capped"
    bps.build(from_snapshot=src, out_dir=out_capped, truth_dir=truth, max_inventory=35)
    raw = (out_capped / "run_0" / "workspace" / "proc" / "locations"
           / "Vienna" / "store_vienna_x.json").read_bytes()
    assert len(raw) <= cap
    json.loads(raw[:cap].decode("utf-8", "replace"))  # full read parses

    out_dense = tmp_path / "dense"
    bps.build(from_snapshot=src, out_dir=out_dense, truth_dir=truth)
    raw_dense = (out_dense / "run_0" / "workspace" / "proc" / "locations"
                 / "Vienna" / "store_vienna_x.json").read_bytes()
    assert len(raw_dense) > cap
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw_dense[:cap].decode("utf-8", "replace"))


# ---- direct comparison against the captured PROD samples -------------------

_TRUTH_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "prod_explore" / "prod_truth"


@pytest.mark.skipif(not (_TRUTH_DIR / "sample_store_0.json").exists(),
                    reason="prod_truth samples not present")
def test_materialized_store_keyset_equals_real_prod_sample(built):
    """The materialized store's top-level key SET must equal a real
    captured PROD store record's key set (the fidelity anchor)."""
    prod = json.loads((_TRUTH_DIR / "sample_store_0.json").read_text())
    mat = json.loads(
        (built / "proc" / "locations" / "Vienna" / "store_vienna_x.json").read_text())
    assert set(mat.keys()) == set(prod.keys())
    # inventory entry keys are a subset of the prod entry shape
    prod_inv_keys = set().union(*[set(e) for e in prod["inventory"]])
    mat_inv_keys = set().union(*[set(e) for e in mat["inventory"]])
    assert mat_inv_keys <= prod_inv_keys
    assert {"sku", "on_hand", "reserved"} <= mat_inv_keys


@pytest.mark.skipif(not (_TRUTH_DIR / "sample_catalog_0.json").exists(),
                    reason="prod_truth samples not present")
def test_materialized_catalog_keyset_equals_real_prod_sample(built):
    prod = json.loads((_TRUTH_DIR / "sample_catalog_0.json").read_text())
    mat = json.loads((built / "proc" / "catalog" / "Makita" / "PT-A.json").read_text())
    assert set(mat.keys()) == set(prod.keys())


@pytest.mark.skipif(not (_TRUTH_DIR / "sample_store_0.json").exists(),
                    reason="prod_truth samples not present")
def test_real_prod_sample_has_no_available_today():
    """Pin the PROD ground truth the materializer mirrors: PROD store
    inventory entries never expose available_today — the agent computes
    max(on_hand-reserved,0). Guards against a future regression that
    re-adds the field to the materializer."""
    prod = json.loads((_TRUTH_DIR / "sample_store_0.json").read_text())
    for e in prod["inventory"]:
        assert "available_today" not in e
        assert "available_today_quantity" not in e
        assert {"sku", "on_hand", "reserved"} <= set(e)
