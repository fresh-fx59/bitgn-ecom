"""Regression tests for the count_per_store completer on the PROD
product_variants schema (revived in v0.1.127 after it was silently dead).

Builds a tiny in-memory db with the real PROD table shapes so we don't
have to commit a multi-MB extracted catalogue.db.
"""
from __future__ import annotations

import sqlite3

import pytest

from bitgn_contest_agent import sku_completer as sc


@pytest.fixture()
def prod_runsql():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE stores (store_id TEXT, store_name TEXT, city TEXT);
        CREATE TABLE product_variants (
            product_sku TEXT, record_path TEXT, brand TEXT, series TEXT,
            model TEXT, product_name TEXT, price_cents INTEGER, properties TEXT
        );
        CREATE TABLE product_variant_properties (
            product_sku TEXT, property_key TEXT,
            property_value_text TEXT, property_value_number TEXT
        );
        CREATE TABLE store_inventory (
            store_id TEXT, product_sku TEXT, available_today_quantity INTEGER
        );
        """
    )
    conn.execute("INSERT INTO stores VALUES ('store_brno_veveri','Veveri','Brno')")
    # Sonax wiper blade: length encoded ONLY in product_name (600mm), not props
    conn.execute(
        "INSERT INTO product_variants VALUES "
        "('AUT-600','/proc/catalog/auto/AUT-600.json','Sonax','Professional',"
        "'XTREME 300-EAF','Sonax Professional XTREME 300-EAF Wiper Blade 600mm','5000','{}')"
    )
    conn.execute(
        "INSERT INTO product_variants VALUES "
        "('AUT-450','/proc/catalog/auto/AUT-450.json','Sonax','Professional',"
        "'XTREME 300-EAF','Sonax Professional XTREME 300-EAF Wiper Blade 450mm','5000','{}')"
    )
    # Mellerud: attribute in the property table
    conn.execute(
        "INSERT INTO product_variants VALUES "
        "('CLN-1','/proc/catalog/clean/CLN-1.json','Mellerud','Universal',"
        "'MEL BO7-35J','Mellerud Universal MEL BO7-35J Cleaning Liquid','1000','{}')"
    )
    conn.executemany(
        "INSERT INTO product_variant_properties VALUES (?,?,?,?)",
        [
            ("CLN-1", "cleaner_type", "glass cleaner", ""),
            ("CLN-1", "volume", "500 ml", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO store_inventory VALUES (?,?,?)",
        [
            ("store_brno_veveri", "AUT-600", 2),
            ("store_brno_veveri", "AUT-450", 9),
            ("store_brno_veveri", "CLN-1", 3),
        ],
    )
    conn.commit()

    def run_sql(q: str):
        try:
            cur = conn.execute(q)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            out = ",".join(cols) + "\n"
            out += "\n".join(",".join("" if v is None else str(v) for v in r) for r in rows)
            return out
        except Exception as e:  # mimic /bin/sql stderr → caller sees no rows
            return f"SQL error: {e}"

    return run_sql


def test_detect_prod_schema(prod_runsql):
    assert (sc._detect_schema(prod_runsql) or {}).get("kind") == "prod"


def test_resolve_store_token_and(prod_runsql):
    # "Veveri PowerTool shop in Brno" → store_brno_veveri via token-AND
    assert sc.resolve_store_id("Veveri PowerTool shop in Brno", prod_runsql) == "store_brno_veveri"


def test_name_encoded_attribute_matches(prod_runsql):
    # length 600 mm is only in product_name → must still resolve the 600mm SKU
    skus = sc._find_qualifying_skus_relaxed(
        brand="Sonax", series="", model="XTREME 300-EAF",
        attributes={"length": "600 mm"}, store_id="store_brno_veveri",
        threshold=1, run_sql=prod_runsql,
    )
    assert skus == ["/proc/catalog/auto/AUT-600.json"]


def test_property_table_attribute_matches(prod_runsql):
    skus = sc._find_qualifying_skus_relaxed(
        brand="Mellerud", series="", model="MEL BO7-35J",
        attributes={"cleaner_type": "glass cleaner", "volume": "500 ml"},
        store_id="store_brno_veveri", threshold=1, run_sql=prod_runsql,
    )
    assert skus == ["/proc/catalog/clean/CLN-1.json"]


def test_negation_task_abstains(prod_runsql):
    class P:
        brand, series, model, attributes = "Sonax", "", "XTREME 300-EAF", {"length": "600 mm"}

    class TS:
        kind = "count_per_store"
        store_descriptor = "Veveri PowerTool shop in Brno"
        threshold = 4
        products = [P()]

    res = sc.complete_sku_refs_from_spec(
        task_spec=TS(), refs=["/AGENTS.MD"], run_sql=prod_runsql,
        task_text="How many of these products have fewer than 4 items available ...",
    )
    assert res.aborted is True
    assert res.added == []


def test_at_least_task_adds_qualifying(prod_runsql):
    class P:
        brand, series, model, attributes = "Sonax", "", "XTREME 300-EAF", {"length": "600 mm"}

    class TS:
        kind = "count_per_store"
        store_descriptor = "Veveri PowerTool shop in Brno"
        threshold = 1
        products = [P()]

    res = sc.complete_sku_refs_from_spec(
        task_spec=TS(), refs=["/AGENTS.MD"], run_sql=prod_runsql,
        task_text="How many of these products have at least 1 items available ...",
    )
    assert res.aborted is False
    assert "/proc/catalog/auto/AUT-600.json" in res.added
