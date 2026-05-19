"""End-to-end SKU completer tests against the
``multi_sku_attr_line_hard`` snapshot's real catalogue.db.

The snapshot's metadata gives us the ground-truth qualifying-SKU
set (required_refs); the completer must enumerate exactly those.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bitgn_contest_agent.sku_completer import (
    ProductSpec,
    complete_sku_refs,
    parse_products,
    parse_store_descriptor,
    parse_threshold,
)


SNAPSHOT = (
    Path(__file__).parent.parent
    / "artifacts"
    / "ws_snapshots"
    / "multi_sku_attr_line_hard"
    / "run_0"
)


@pytest.fixture(scope="module")
def snapshot_db():
    """Open the snapshot's catalogue.db AND seed a synthetic
    `stores` table that maps the snapshot's store_id to a row."""
    src = SNAPSHOT / "workspace" / "catalogue.db"
    # Copy via in-memory by dumping schema+data so writes stay
    # transient.
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(":memory:")
    src_conn.backup(dst_conn)
    src_conn.close()
    # The catalogue.db doesn't have a `stores` table; seed it from
    # the inventory's unique store_ids so resolve_store_id works.
    cur = dst_conn.cursor()
    cur.execute(
        "CREATE TABLE stores (id TEXT PRIMARY KEY, lat REAL, lon REAL)"
    )
    cur.execute(
        "INSERT INTO stores VALUES ('store_acmetown_central', 50.0, 14.0)"
    )
    dst_conn.commit()
    yield dst_conn
    dst_conn.close()


@pytest.fixture(scope="module")
def snapshot_metadata():
    with open(SNAPSHOT / "metadata.json") as f:
        return json.load(f)


def _sql_runner(conn):
    """Mirror /bin/sql: CSV-in-JSON envelope output."""
    def run_sql(sql: str) -> str:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        body_lines = [",".join(cols)] if cols else []
        for r in rows:
            body_lines.append(
                ",".join("" if v is None else str(v) for v in r)
            )
        body = "\n".join(body_lines) + "\n"
        return json.dumps({"stdout": body, "stderr": "", "exit_code": 0})
    return run_sql


# ── parsing ──────────────────────────────────────────────────────────


def test_parse_threshold():
    s = (
        "How many of these products have at least 1 items available "
        "in Acmetown Central hardware store today: ..."
    )
    assert parse_threshold(s) == 1


def test_parse_threshold_higher():
    s = "How many of these products have at least 3 items available ..."
    assert parse_threshold(s) == 3


def test_parse_store_descriptor():
    s = (
        "How many of these products have at least 1 items available "
        "in Acmetown Central hardware store today: ..."
    )
    assert parse_store_descriptor(s) == "Acmetown Central"


def test_parse_products_single(snapshot_metadata):
    """Parsing the multi_sku_attr_line_hard instruction yields 6
    product specs (the metadata's expected_answer = 3 means 3 of
    those 6 qualify)."""
    known_keys = {
        "voltage", "battery_platform", "kit_contents",
        "fastener_type", "diameter", "length", "pack_count",
        "anchor_type", "disc_diameter",
        "fitting_type", "connection_type",
    }
    products = parse_products(
        snapshot_metadata["instruction"], known_keys=known_keys,
    )
    assert len(products) == 6
    # Spot-check the first spec
    p0 = products[0]
    assert p0.brand == "Acmetool"
    assert "Pro Z9" in p0.line_text
    assert p0.attributes.get("voltage") == "18 V"
    assert p0.attributes.get("battery_platform") == "18v-system"
    assert p0.attributes.get("kit_contents") == "case"


# ── end-to-end completer ─────────────────────────────────────────────


def test_completer_adds_missing_qualifying_skus(snapshot_db, snapshot_metadata):
    """Reproduce the t14/t15/t16 wrong-SKU-pick PROD failure:
    agent cited only the store anchor (or wrong SKUs entirely), the
    completer should ADD the 3 qualifying SKUs from required_refs."""
    # Simulate the worst-case agent: only the store was cited.
    refs = ["/proc/stores/store_acmetown_central.json"]
    res = complete_sku_refs(
        task_text=snapshot_metadata["instruction"],
        refs=refs,
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is False
    # Every required SKU path must now be in refs.
    for required in snapshot_metadata["required_refs"]:
        if required.startswith("/proc/catalog/"):
            assert (
                required in res.refs
            ), f"completer missed required {required}"


def test_completer_keeps_existing_correct_refs(snapshot_db, snapshot_metadata):
    """When the agent already cited the correct SKUs, completer
    is a no-op for those refs."""
    refs = list(snapshot_metadata["required_refs"])
    res = complete_sku_refs(
        task_text=snapshot_metadata["instruction"],
        refs=refs,
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is False
    # All originals still present
    for r in refs:
        assert r in res.refs


def test_completer_abstains_on_non_count_task(snapshot_db):
    res = complete_sku_refs(
        task_text="Apply a 10% discount to basket_001.",
        refs=["/proc/baskets/basket_001.json"],
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is True


def test_completer_abstains_on_missing_threshold(snapshot_db):
    res = complete_sku_refs(
        task_text=(
            "How many of these products: the Cordless Drill Driver "
            "from Acmetool in the Acmetool Pro Z9 line that has "
            "voltage 18 V?"
        ),
        refs=[],
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is True


def test_completer_abstains_when_store_unresolvable(snapshot_db, snapshot_metadata):
    # Replace store name with gibberish
    task = snapshot_metadata["instruction"].replace(
        "Acmetown Central", "Atlantis Imaginary Mountain"
    )
    res = complete_sku_refs(
        task_text=task,
        refs=[],
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is True


def test_completer_does_not_add_disqualifying_skus(snapshot_db, snapshot_metadata):
    """SKUs with mismatched attributes or insufficient inventory
    must NOT be added. The metadata's forbidden_refs are the
    same-line wrong-attribute variants."""
    refs: list[str] = []
    res = complete_sku_refs(
        task_text=snapshot_metadata["instruction"],
        refs=refs,
        run_sql=_sql_runner(snapshot_db),
    )
    for forbidden in snapshot_metadata["forbidden_refs"]:
        assert forbidden not in res.refs, (
            f"completer wrongly added forbidden {forbidden}"
        )
