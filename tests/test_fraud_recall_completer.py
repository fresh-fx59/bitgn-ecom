"""End-to-end fraud_recall_completer tests against the
t40_v155_fail snapshot's SQLite. Asserts the completer recovers
the full 22-row canonical set even when the agent under-called."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bitgn_contest_agent.fraud_recall_completer import (
    complete_fraud_refs,
)


SNAPSHOT_ROOT = (
    Path(__file__).parent.parent
    / "artifacts"
    / "ws_snapshots"
    / "t40_v155_fail"
    / "run_0"
    / "workspace"
)


def _load_payments():
    payments_dir = SNAPSHOT_ROOT / "proc" / "payments"
    rows = []
    for f in sorted(payments_dir.glob("pay_*.json")):
        with open(f) as fh:
            rows.append(json.load(fh))
    return rows


def _build_db():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE payments (id TEXT PRIMARY KEY, basket_id TEXT,"
        " basket_archived INTEGER, customer_id TEXT, store_id TEXT,"
        " amount_cents INTEGER, currency TEXT, status TEXT,"
        " created_at TEXT, payment_method_fingerprint TEXT,"
        " device_fingerprint TEXT, observed_lat REAL, observed_lon REAL,"
        " path TEXT)"
    )
    cur.execute("CREATE TABLE stores (id TEXT PRIMARY KEY, lat REAL, lon REAL)")
    for p in _load_payments():
        cur.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["id"], p.get("basket_id"),
                1 if p.get("basket_archived") else 0,
                p["customer_id"], p["store_id"], p["amount_cents"],
                p.get("currency"), p.get("status"), p["created_at"],
                p["payment_method_fingerprint"], p["device_fingerprint"],
                p["observed_lat"], p["observed_lon"],
                f"/proc/payments/{p['id']}.json",
            ),
        )
    distinct_stores = sorted({p["store_id"] for p in _load_payments()})
    for i, sid in enumerate(distinct_stores):
        cur.execute(
            "INSERT INTO stores VALUES (?, ?, ?)",
            (sid, 50.0 + i * 0.5, 14.0 + i * 0.5),
        )
    conn.commit()
    return conn


def _sql_runner(conn):
    def run_sql(sql):
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        body_lines = [",".join(cols)] if cols else []
        for r in rows:
            body_lines.append(",".join("" if v is None else str(v) for v in r))
        body = "\n".join(body_lines) + "\n"
        return json.dumps({"stdout": body, "stderr": "", "exit_code": 0})
    return run_sql


@pytest.fixture(scope="module")
def snapshot_db():
    conn = _build_db()
    yield conn
    conn.close()


def test_completer_returns_22_canonical_rows(snapshot_db):
    """The canonical set should equal the 22 true-fraud rows
    (cust_068 12 + cust_031..035 2 each), excluding the 3 cust_025
    single-device FPs."""
    from bitgn_contest_agent.fraud_recall_completer import (
        fetch_canonical_fraud_set,
    )
    paths = fetch_canonical_fraud_set(_sql_runner(snapshot_db))
    assert paths is not None
    assert len(paths) == 22
    # cust_025 paths excluded
    cust_025_ids = {
        f"/proc/payments/{p['id']}.json"
        for p in _load_payments()
        if p["customer_id"] == "cust_025"
    }
    for cid in cust_025_ids:
        assert cid not in paths


def test_completer_adds_missing_rows_when_agent_undercalls(snapshot_db):
    """v0.1.75/v0.1.80/v0.1.87 t40 PROD failure mode: agent only
    found cust_068's 12-row burst. Completer adds the remaining 10
    cluster members from 2021-05-06."""
    # Simulate agent cited only cust_068 rows
    cust_068_paths = [
        f"/proc/payments/{p['id']}.json"
        for p in _load_payments()
        if p["customer_id"] == "cust_068"
    ]
    assert len(cust_068_paths) == 12
    res = complete_fraud_refs(
        task_text=(
            "We have a confirmed fraud incident in archived payment "
            "history. Identify the fraudulent payment records."
        ),
        refs=cust_068_paths,
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is False
    # 22 canonical - 12 already cited = 10 added
    assert len(res.added) == 10


def test_completer_no_op_on_non_fraud_task(snapshot_db):
    res = complete_fraud_refs(
        task_text="Apply a discount to basket_001.",
        refs=[],
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.aborted is True
    assert res.added == []


def test_completer_abstains_on_sql_failure():
    res = complete_fraud_refs(
        task_text="confirmed fraud incident; identify payments",
        refs=[],
        run_sql=lambda sql: None,
    )
    assert res.aborted is True


def test_completer_no_op_when_agent_already_cited_all(snapshot_db):
    """When agent already submitted the canonical 22 rows, no add."""
    all_paths = [
        f"/proc/payments/{p['id']}.json"
        for p in _load_payments()
        if p["customer_id"] != "cust_025"
    ]
    res = complete_fraud_refs(
        task_text="fraud incident",
        refs=all_paths,
        run_sql=_sql_runner(snapshot_db),
    )
    assert res.added == []
