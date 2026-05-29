"""Validate the fraud-ring completer against the faithful t40 snapshot.

Builds in-memory SQLite from the extracted PROD-schema snapshot and runs
the completer's exact SQL. The derived t40 oracle is the 26-payment
device∪method connected component (= all archived payments of the
compromised customer); the agent's device-only detection catches 24, so
the completer must ADD the 2 it misses. See memory project_ecom_fraud_structure.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bitgn_contest_agent.fraud_component_completer import (
    complete_fraud_refs,
    looks_like_sql_fraud_task,
    resolve_fraud_component,
)

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "artifacts" / "ws_snapshots" / "t40_real2"


def _build_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript((SNAP / "sql_schema.sql").read_text())
    for jf in (SNAP / "sql").glob("*.json"):
        rows = json.loads(jf.read_text())
        if not rows:
            continue
        cols = list(rows[0].keys())
        conn.executemany(
            f'INSERT INTO "{jf.stem}" ({",".join(chr(34)+c+chr(34) for c in cols)}) '
            f'VALUES ({",".join("?" for _ in cols)})',
            [tuple(r.get(c) for c in cols) for r in rows],
        )
    conn.commit()
    return conn


def _run_sql(conn):
    def run_sql(sql):
        try:
            cur = conn.execute(sql)
        except sqlite3.Error:
            return None
        rows = cur.fetchall()
        header = "|".join(d[0] for d in cur.description) if cur.description else ""
        return "\n".join([header] + ["|".join("" if c is None else str(c) for c in r) for r in rows])
    return run_sql


def _instruction() -> str:
    return json.loads((SNAP / "run_0" / "metadata.json").read_text())["instruction"]


def _skip():
    if not (SNAP / "sql_schema.sql").exists():
        pytest.skip("no faithful t40 snapshot")


def test_detects_sql_fraud_task():
    _skip()
    assert looks_like_sql_fraud_task(_instruction())
    # t48-style file task must NOT match (different path)
    assert not looks_like_sql_fraud_task(
        "Read /archive/payment_batch_export_X.tsv and find fraud incidents"
    )


def test_resolves_full_26_component():
    _skip()
    comp = resolve_fraud_component(_run_sql(_build_db()), _instruction())
    assert len(comp) == 26, f"expected 26-payment component, got {len(comp)}"


def test_adds_the_two_under_cited_when_agent_has_device_cluster_24():
    _skip()
    # Simulate the agent citing only the 24 device-cluster payments.
    conn = _build_db()
    rows = [r for r in json.loads((SNAP / "sql" / "payment_transactions.json").read_text())
            if str(r.get("is_archived_basket_reference")) in ("1", "True", "true")
            and r.get("device_fingerprint") == "dev_4u7KsGBWmhLDqU"]
    agent_cited = [r["record_path"] for r in rows]
    assert len(agent_cited) == 24
    added = complete_fraud_refs(_run_sql(conn), _instruction(), agent_cited)
    # adds exactly the 2 ring members on the secondary device/method
    assert len(added) == 2, f"expected +2, got {len(added)}: {added}"


def test_abstains_without_clear_anomaly():
    _skip()
    # A DB where every device appears equally → no dominant anomaly.
    conn = sqlite3.connect(":memory:")
    conn.executescript((SNAP / "sql_schema.sql").read_text())
    for i in range(10):
        conn.execute(
            "INSERT INTO payment_transactions "
            "(payment_id, record_path, is_archived_basket_reference, "
            "device_fingerprint, payment_method_fingerprint) "
            "VALUES (?,?,?,?,?)",
            (f"pay_{i}", f"/p/{i}.json", 1, f"dev_{i}", f"pm_{i}"),
        )
    conn.commit()
    assert resolve_fraud_component(_run_sql(conn), _instruction()) == []


def test_abstains_on_tsv_task():
    _skip()
    added = complete_fraud_refs(
        _run_sql(_build_db()),
        "Read /archive/payment_batch_export_X.tsv and find fraud incidents",
        [],
    )
    assert added == []
