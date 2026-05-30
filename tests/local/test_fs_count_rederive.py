"""Tests for filesystem count_per_store re-derivation (PROD no-SQL path).

Builds a tiny PROD-faithful snapshot via build_prod_snapshot, then drives
rederive_count_fs through LocalEcomClient read/search/list callbacks and
checks: correct count, store/availability convention (max(on_hand-reserved,0),
absent SKU = 0), and ABSTAIN on ambiguity (never a wrong bounce).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bitgn_contest_agent.fs_count_rederive import rederive_count_fs
from bitgn_contest_agent.local.ecom_client import LocalEcomClient
from bitgn_contest_agent.schemas import ProductFilter, TaskSpec

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("build_prod_snapshot", _SCRIPTS / "build_prod_snapshot.py")
bps = importlib.util.module_from_spec(_spec)
sys.modules["build_prod_snapshot"] = bps
_spec.loader.exec_module(bps)


def _src_db(root: Path) -> Path:
    ws = root / "run_0" / "workspace"
    ws.mkdir(parents=True)
    (root / "run_0" / "metadata.json").write_text(json.dumps({"instruction": "x"}))
    conn = sqlite3.connect(str(ws / "catalogue.db"))
    conn.execute("CREATE TABLE stores (store_id TEXT, record_path TEXT, store_name TEXT, city TEXT, is_open INT, latitude REAL, longitude REAL)")
    conn.execute("INSERT INTO stores VALUES ('store_brno_veveri','/p','PowerTool Brno Veveri','Brno',1,49.2,16.6)")
    conn.execute("CREATE TABLE store_inventory (store_id TEXT, product_sku TEXT, on_hand_quantity INT, reserved_quantity INT, available_today_quantity INT, incoming_quantity INT, next_arrival_in_days INT)")
    # GLO-V1 in stock (5-2=3 avail); HONXL absent from inventory (=0)
    conn.execute("INSERT INTO store_inventory VALUES ('store_brno_veveri','GLO-V1',5,2,3,0,0)")
    conn.execute("CREATE TABLE product_variants (product_sku TEXT, record_path TEXT, brand TEXT, product_name TEXT, price_cents INT, price_currency TEXT, product_category_id TEXT, product_kind_id TEXT, product_family_id TEXT, properties TEXT)")
    # Honeywell KJB-LZD line: XL/Orange (target, absent from inv -> 0) and L/Yellow (off-spec)
    conn.execute("INSERT INTO product_variants VALUES ('HONXL','/p','Honeywell','Honeywell Miller Howard KJB-LZD Work Gloves','1','EUR','c','k','f','{\"size\":\"XL\",\"color_family\":\"Orange\",\"protection_class\":\"cut-3\"}')")
    conn.execute("INSERT INTO product_variants VALUES ('HONL','/p','Honeywell','Honeywell Miller Howard KJB-LZD Work Gloves','1','EUR','c','k','f','{\"size\":\"L\",\"color_family\":\"Yellow\",\"protection_class\":\"cut-3\"}')")
    # Gorilla HL7-T2E masking tape: GLO-V1 in stock
    conn.execute("INSERT INTO product_variants VALUES ('GLO-V1','/p','Gorilla','Gorilla Heavy Duty Grip HL7-T2E Tape and Foam','1','EUR','c','k','f','{\"product_type\":\"masking tape\"}')")
    conn.commit(); conn.close()
    return root


@pytest.fixture
def client(tmp_path):
    src = _src_db(tmp_path / "src")
    truth = tmp_path / "truth"; (truth / "docs").mkdir(parents=True)
    (truth / "AGENTS.MD").write_text("prod")
    out = tmp_path / "prod"
    bps.build(from_snapshot=src, out_dir=out, truth_dir=truth)
    return LocalEcomClient(out / "run_0" / "workspace")


def _callbacks(client):
    def read_fn(p):
        try:
            return client.read(SimpleNamespace(path=p, start_line=0, end_line=0, number=False)).content
        except Exception:
            return None
    def search_fn(root, pat):
        r = client.search(SimpleNamespace(root=root, pattern=pat, limit=50))
        return [m.path for m in r.matches]
    def list_fn(p):
        try:
            return [e.path for e in client.list(SimpleNamespace(path=p)).entries]
        except Exception:
            return []
    return read_fn, search_fn, list_fn


def _spec_count(threshold_text, products):
    return TaskSpec(kind="count_per_store", store_descriptor="Veveri PowerTool shop in Brno",
                    threshold=1, products=products)


def test_count_avail_ge1_gorilla_instock_honeywell_zero(client):
    read_fn, search_fn, list_fn = _callbacks(client)
    ts = _spec_count(1, [
        ProductFilter(brand="Honeywell", model="KJB-LZD", name="Work Gloves",
                      attributes={"size": "XL", "color_family": "Orange", "protection_class": "cut-3"}),
        ProductFilter(brand="Gorilla", model="HL7-T2E", name="Tape and Foam",
                      attributes={"product_type": "masking tape"}),
    ])
    task_text = "How many of these have at least 1 items available in the Veveri PowerTool shop in Brno today: ..."
    rr = rederive_count_fs(ts, read_fn, search_fn, list_fn, task_text)
    # Honeywell XL/Orange absent from store inventory -> 0 (not qualifying);
    # Gorilla GLO-V1 = max(5-2,0)=3 >=1 -> qualifies. Count = 1.
    assert rr.count == 1, rr.reason
    verdicts = {b: q for (b, c, q) in rr.per_product}
    assert verdicts["Honeywell"] is False
    assert verdicts["Gorilla"] is True


def test_abstains_on_unresolved_store(client):
    read_fn, search_fn, list_fn = _callbacks(client)
    ts = _spec_count(1, [ProductFilter(brand="Gorilla", model="HL7-T2E", attributes={})])
    ts.store_descriptor = "Nonexistent Atlantis shop"
    rr = rederive_count_fs(ts, read_fn, search_fn, list_fn, "at least 1 available")
    assert rr.count is None  # ABSTAIN, no bounce


def test_abstains_on_unresolved_product(client):
    read_fn, search_fn, list_fn = _callbacks(client)
    ts = _spec_count(1, [ProductFilter(brand="Bosch", model="ZZZ-999", attributes={})])
    rr = rederive_count_fs(ts, read_fn, search_fn, list_fn, "at least 1 available")
    assert rr.count is None  # line unresolved -> abstain


def test_not_count_kind_abstains(client):
    read_fn, search_fn, list_fn = _callbacks(client)
    ts = TaskSpec(kind="none")
    rr = rederive_count_fs(ts, read_fn, search_fn, list_fn, "anything")
    assert rr.count is None
