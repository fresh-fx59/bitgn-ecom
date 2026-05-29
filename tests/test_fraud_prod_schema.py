"""Regression tests for the fraud enforcers on the PROD
payment_transactions schema (revived in v0.1.133 after they were
silently dead — they targeted the legacy `payments` schema).

Builds a tiny in-memory PROD-schema db with one coordinated fraud
cluster (2 accounts sharing a device, geo-impossible cross-store
burst), one legit single-device burster, and one legit multi-device
customer outside the cluster.
"""
from __future__ import annotations

import sqlite3

import pytest

from bitgn_contest_agent import fraud_recall_completer as fr
from bitgn_contest_agent import fraud_cluster_filter as fcf


@pytest.fixture()
def prod_runsql():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE stores (store_id TEXT, latitude REAL, longitude REAL);
        CREATE TABLE payment_transactions (
            payment_id TEXT, record_path TEXT, is_archived_basket_reference INTEGER,
            customer_id TEXT, store_id TEXT, payment_amount_cents INTEGER,
            payment_created_at TEXT, payment_method_fingerprint TEXT,
            device_fingerprint TEXT, observed_latitude REAL, observed_longitude REAL
        );
        """
    )
    conn.execute("INSERT INTO stores VALUES ('store_a',48.2,16.3)")
    conn.execute("INSERT INTO stores VALUES ('store_b',47.0,15.4)")
    rows = []
    # Fraud cluster: cust_f1 + cust_f2 share device dev_X, cross-store
    # within minutes (geo-impossible), each uses 2 devices in the burst.
    base = "2021-05-06T11:0{}:00Z"
    for i, (cid, dev, store) in enumerate([
        ("cust_f1", "dev_X", "store_a"), ("cust_f1", "dev_Y", "store_b"),
        ("cust_f2", "dev_X", "store_b"), ("cust_f2", "dev_Z", "store_a"),
    ]):
        pid = f"pay_f{i}"
        rows.append((pid, f"/proc/payments/{pid}.json", 1, cid, store, 5000,
                     base.format(i), "pm_shared", dev, 48.2 if store == "store_a" else 47.0,
                     16.3 if store == "store_a" else 15.4))
    # Legit single-device burster (should be dropped: 1 device)
    for i in range(2):
        pid = f"pay_s{i}"
        rows.append((pid, f"/proc/payments/{pid}.json", 1, "cust_s", "store_a", 3000,
                     f"2021-05-06T11:0{i}:30Z", "pm_s", "dev_S", 48.2, 16.3))
    # Legit multi-device customer OUTSIDE the cluster (spread over weeks)
    for i, day in enumerate(["2021-03-01", "2021-04-15"]):
        pid = f"pay_l{i}"
        rows.append((pid, f"/proc/payments/{pid}.json", 1, "cust_l", "store_a", 4000,
                     f"{day}T10:00:00Z", "pm_l", f"dev_L{i}", 48.2, 16.3))
    conn.executemany(
        "INSERT INTO payment_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()

    def run_sql(q: str):
        try:
            cur = conn.execute(q)
            rs = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            out = ",".join(cols) + "\n"
            out += "\n".join(",".join("" if v is None else str(v) for v in r) for r in rs)
            return out
        except Exception as e:
            return f"SQL error: {e}"

    return run_sql


def test_detect_prod_payments_schema(prod_runsql):
    s = fr.detect_payments_schema(prod_runsql)
    assert s and s["tbl"] == "payment_transactions"
    assert s["id"] == "payment_id" and s["arch"] == "is_archived_basket_reference"


def test_canonical_set_is_the_cluster(prod_runsql):
    canon = fr.fetch_canonical_fraud_set(prod_runsql)
    ids = {p.split("/")[-1].replace(".json", "") for p in (canon or [])}
    # the 4 fraud-cluster rows, none of the legit ones
    assert ids == {"pay_f0", "pay_f1", "pay_f2", "pay_f3"}, ids


def test_filter_keeps_cluster_drops_fps(prod_runsql):
    cited = [f"/proc/payments/pay_f{i}.json" for i in range(4)]
    cited += ["/proc/payments/pay_s0.json", "/proc/payments/pay_l0.json", "/AGENTS.MD"]
    res = fcf.filter_fraud_refs(
        task_text="confirmed fraud incident in archived payment history",
        refs=cited, run_sql=prod_runsql,
    )
    kept = set(res.refs)
    for i in range(4):
        assert f"/proc/payments/pay_f{i}.json" in kept  # cluster kept
    assert "/proc/payments/pay_s0.json" not in kept  # single-device dropped
    assert "/proc/payments/pay_l0.json" not in kept  # out-of-cluster dropped
    assert "/AGENTS.MD" in kept  # non-payment untouched
