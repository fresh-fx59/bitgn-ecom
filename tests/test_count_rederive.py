"""Oracle tests for deterministic count_per_store re-derivation.

Builds an in-memory sqlite from the PROD-faithful snapshot DBs
(``artifacts/ws_snapshots/<t>/sql_schema.sql`` + ``sql/*.json``) and a
``run_sql`` shim, then asserts the re-derivation reproduces the two
controller-verified oracles (t45→4, t16→3) and abstains on ambiguity.
"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest

from bitgn_contest_agent.count_rederive import rederive_count

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "artifacts" / "ws_snapshots"


def _build_db(snap_dir):
    schema = (snap_dir / "sql_schema.sql").read_text()
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    for jf in (snap_dir / "sql").glob("*.json"):
        rows = json.loads(jf.read_text())
        if not rows:
            continue
        cols = list(rows[0].keys())
        conn.executemany(
            f'INSERT INTO "{jf.stem}" ({",".join(chr(34) + c + chr(34) for c in cols)}) '
            f'VALUES ({",".join("?" for _ in cols)})',
            [tuple(r.get(c) for c in cols) for r in rows],
        )
    conn.commit()
    return conn


def _make_run_sql(conn):
    def run_sql(sql):
        try:
            cur = conn.execute(sql)
        except sqlite3.Error:
            return None
        rows = cur.fetchall()
        header = "|".join(d[0] for d in cur.description) if cur.description else ""
        return "\n".join(
            [header]
            + ["|".join("" if c is None else str(c) for c in r) for r in rows]
        )

    return run_sql


def _P(brand, code, attributes):
    o = types.SimpleNamespace()
    o.brand, o.model, o.series, o.name, o.attributes = (
        brand,
        code,
        "",
        "",
        dict(attributes),
    )
    return o


def _spec(store_descriptor, products):
    o = types.SimpleNamespace()
    o.kind, o.store_descriptor, o.products = (
        "count_per_store",
        store_descriptor,
        products,
    )
    return o


def _instruction(t):
    meta = json.loads((SNAP / t / "run_0" / "metadata.json").read_text())
    return meta["instruction"]


# ── verified product lists (CONTROLLER-VERIFIED FACTS) ───────────────

_T45_PRODUCTS = [
    _P("Fiskars", "1CD-A3X", {"power_source": "battery"}),
    _P("Mobil", "1ZE-TCR", {"volume": "5000 ml", "viscosity": "15W-40"}),
    _P(
        "Keter",
        "2OO-VJU",
        {
            "storage_type": "parts case",
            "color_family": "Yellow",
            "volume": "8 l",
        },
    ),
    _P("Engelbert Strauss", "37H-N9K", {"color_family": "Black"}),
    _P(
        "Sika",
        "28T-UV8",
        {
            "sealant_type": "hybrid sealant",
            "color_family": "Gray",
            "volume": "300 ml",
        },
    ),
    _P("Sonax", "304-ZK0", {"length": "450 mm"}),
]

_T16_PRODUCTS = [
    _P(
        "Honeywell",
        "KJB-LZD",
        {"size": "XL", "color_family": "Orange", "protection_class": "cut-3"},
    ),
    _P("Gorilla", "HL7-T2E", {"product_type": "masking tape"}),
    _P("Hager", "29D-PDT", {"color_family": "White", "length": "2 m"}),
    _P("Sonax", "300-EAF", {"length": "600 mm"}),
    _P(
        "Mellerud",
        "BO7-35J",
        {"cleaner_type": "glass cleaner", "volume": "500 ml"},
    ),
    _P(
        "AlcaPlast",
        "33F-7U9",
        {"connector_type": "shower hose", "diameter": "12 mm"},
    ),
]


def test_t45_rederives_to_4():
    conn = _build_db(SNAP / "t45_real2")
    run_sql = _make_run_sql(conn)
    spec = _spec("Wilten PowerTool store in Innsbruck", _T45_PRODUCTS)
    res = rederive_count(spec, run_sql, _instruction("t45_real2"))
    assert res.count == 4, (res.count, res.reason, res.per_product)


def test_t16_rederives_to_3():
    conn = _build_db(SNAP / "t16_real2")
    run_sql = _make_run_sql(conn)
    spec = _spec("Veveri PowerTool shop in Brno", _T16_PRODUCTS)
    res = rederive_count(spec, run_sql, _instruction("t16_real2"))
    assert res.count == 3, (res.count, res.reason, res.per_product)


def test_abstains_on_unparseable_threshold():
    res = rederive_count(
        _spec("x", [_P("B", "C", {})]),
        lambda s: "x\n",
        "no comparator here",
    )
    assert res.count is None


def test_abstains_on_non_count_spec():
    o = types.SimpleNamespace()
    o.kind, o.store_descriptor, o.products = "none", "x", [_P("B", "C", {})]
    res = rederive_count(o, lambda s: "x\n", "fewer than 4 items available")
    assert res.count is None


def test_abstains_when_store_unresolved():
    conn = _build_db(SNAP / "t45_real2")
    run_sql = _make_run_sql(conn)
    spec = _spec("Nowhere Fake store in Atlantis", _T45_PRODUCTS)
    res = rederive_count(spec, run_sql, _instruction("t45_real2"))
    assert res.count is None
