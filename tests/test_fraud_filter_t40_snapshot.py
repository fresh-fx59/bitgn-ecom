"""End-to-end fraud filter test against the rebuilt t40 snapshot.

Builds an in-memory SQLite from the snapshot's 25 payment JSON
records, then runs `filter_fraud_refs` exactly as the agent would
in PROD — including the CSV-in-JSON envelope format that
`/bin/sql` returns. Asserts the filter drops the 3 cust_025
single-device FPs (sum EUR 125.50) and keeps the 22 multi-device
true fraud rows.

This is the local fixture the user flagged as missing: future fraud-
filter iterations cost $0 to validate instead of $15 per PROD run.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bitgn_contest_agent.fraud_cluster_filter import filter_fraud_refs


SNAPSHOT_ROOT = (
    Path(__file__).parent.parent
    / "artifacts"
    / "ws_snapshots"
    / "t40_v155_fail"
    / "run_0"
    / "workspace"
)


def _load_payments() -> list[dict]:
    """Read all /proc/payments/*.json from the snapshot."""
    payments_dir = SNAPSHOT_ROOT / "proc" / "payments"
    rows = []
    for f in sorted(payments_dir.glob("pay_*.json")):
        with open(f) as fh:
            rows.append(json.load(fh))
    return rows


def _build_db() -> sqlite3.Connection:
    """Construct an in-memory SQLite mirroring the contest's
    `payments` and `stores` tables for the snapshot's data."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE payments (
            id TEXT PRIMARY KEY,
            basket_id TEXT,
            basket_archived INTEGER,
            customer_id TEXT,
            store_id TEXT,
            amount_cents INTEGER,
            currency TEXT,
            status TEXT,
            created_at TEXT,
            payment_method_fingerprint TEXT,
            device_fingerprint TEXT,
            observed_lat REAL,
            observed_lon REAL,
            path TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE stores (
            id TEXT PRIMARY KEY,
            lat REAL,
            lon REAL
        )
        """
    )
    payments = _load_payments()
    for p in payments:
        cur.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["id"],
                p.get("basket_id"),
                1 if p.get("basket_archived") else 0,
                p["customer_id"],
                p["store_id"],
                p["amount_cents"],
                p.get("currency"),
                p.get("status"),
                p["created_at"],
                p["payment_method_fingerprint"],
                p["device_fingerprint"],
                p["observed_lat"],
                p["observed_lon"],
                f"/proc/payments/{p['id']}.json",
            ),
        )
    # Stores at synthetic coords far from the observed fraud coords
    # (so the coord-cluster-not-near-store check fires).
    distinct_stores = sorted({p["store_id"] for p in payments})
    for i, sid in enumerate(distinct_stores):
        cur.execute(
            "INSERT INTO stores VALUES (?, ?, ?)",
            (sid, 50.0 + i * 0.5, 14.0 + i * 0.5),
        )
    conn.commit()
    return conn


def _sql_runner(conn: sqlite3.Connection):
    """Return a run_sql callable that mirrors /bin/sql's output
    shape: CSV inside a JSON envelope ``{"stdout": "...", "stderr":
    "", "exit_code": 0}``."""

    def run_sql(sql: str) -> str:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        body_lines = [",".join(cols)]
        for r in rows:
            body_lines.append(",".join("" if v is None else str(v) for v in r))
        body = "\n".join(body_lines) + "\n"
        return json.dumps(
            {"stdout": body, "stderr": "", "exit_code": 0}
        )

    return run_sql


@pytest.fixture(scope="module")
def snapshot_db():
    conn = _build_db()
    yield conn
    conn.close()


def test_snapshot_has_25_payments():
    assert len(_load_payments()) == 25


def test_fraud_filter_drops_3_cust_025_fps(snapshot_db):
    """The headline end-to-end test: filter drops exactly the 3
    cust_025 single-device FPs and keeps the 22 multi-device true
    fraud rows. Reproduces the v0.1.78 PROD t40 = 1.0 result locally.
    """
    payments = _load_payments()
    refs = [f"/proc/payments/{p['id']}.json" for p in payments]

    res = filter_fraud_refs(
        task_text=(
            "We have a confirmed fraud incident in archived payment "
            "history. Find the payment records that are part of the "
            "incident. Do not modify anything; cite each payment "
            "record you classify as fraud."
        ),
        refs=refs,
        run_sql=_sql_runner(snapshot_db),
    )
    # cust_025's three payments must be dropped
    cust_025_ids = [
        p["id"]
        for p in payments
        if p["customer_id"] == "cust_025"
    ]
    assert len(cust_025_ids) == 3
    for pid in cust_025_ids:
        assert (
            f"/proc/payments/{pid}.json" in res.dropped
        ), f"expected {pid} dropped"
    # All 22 non-cust_025 payments must be kept
    other_ids = [
        p["id"]
        for p in payments
        if p["customer_id"] != "cust_025"
    ]
    assert len(other_ids) == 22
    for pid in other_ids:
        assert (
            f"/proc/payments/{pid}.json" in res.refs
        ), f"expected {pid} kept"


def test_fraud_filter_emits_three_drop_reasons(snapshot_db):
    payments = _load_payments()
    refs = [f"/proc/payments/{p['id']}.json" for p in payments]
    res = filter_fraud_refs(
        task_text="confirmed fraud incident; identify payment records",
        refs=refs,
        run_sql=_sql_runner(snapshot_db),
    )
    assert len(res.dropped) == 3
    assert all(
        "only 1 device" in reason or "1 device(s)" in reason
        for reason in res.reasons
    )


def test_filter_aborts_gracefully_on_sql_error(snapshot_db):
    def broken_sql(_):
        raise RuntimeError("simulated SQL failure")

    payments = _load_payments()
    refs = [f"/proc/payments/{p['id']}.json" for p in payments[:5]]
    # filter_fraud_refs catches None return → aborts → refs unchanged
    res = filter_fraud_refs(
        task_text="confirmed fraud incident",
        refs=refs,
        run_sql=lambda sql: None,
    )
    assert res.aborted is True
    assert len(res.refs) == 5
    assert res.dropped == []
