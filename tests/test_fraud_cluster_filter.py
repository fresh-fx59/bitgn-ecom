"""Tests for fraud_cluster_filter (v0.1.75 distinct-device discriminator)."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.fraud_cluster_filter import (
    PaymentRow,
    WINDOW_SECONDS,
    _build_cluster_membership,
    _parse_iso,
    filter_fraud_refs,
    looks_like_fraud_task,
)


def _t(s: str) -> float:
    return _parse_iso(s)


# ── timestamp parsing ────────────────────────────────────────────────


def test_parse_iso_z_suffix():
    assert _parse_iso("2021-04-28T14:38:38Z") is not None


def test_parse_iso_offset():
    assert _parse_iso("2021-04-28T14:38:38+00:00") is not None


def test_parse_iso_compact():
    assert _parse_iso("20210428T143838Z") is not None


def test_parse_iso_invalid_returns_none():
    assert _parse_iso("not-a-date") is None


# ── cluster builder (legacy helper) ──────────────────────────────────


def test_cluster_pair_same_customer_diff_store_within_window():
    rows = [
        PaymentRow("pay_1", "cust_A", "store_X", _t("2021-04-28T14:00:00Z")),
        PaymentRow("pay_2", "cust_A", "store_Y", _t("2021-04-28T14:05:00Z")),
    ]
    assert _build_cluster_membership(rows) == {"pay_1", "pay_2"}


# ── distinct-device filter (current API) ─────────────────────────────


def _sql_output(rows: dict[str, tuple[int, int, int, int, int]]) -> str:
    """Format: '<pid>|<n_total>|<n_id_share>|<in_time>|<in_coord>|<cust_devs>'."""
    return "\n".join(
        f"{pid}|{n_total}|{n_id_share}|{in_time}|{in_coord}|{cust_dev}"
        for pid, (n_total, n_id_share, in_time, in_coord, cust_dev) in rows.items()
    ) + "\n"


def test_filter_keeps_multi_device_customer():
    """True fraud — attacker switches devices."""
    rows = {
        "pay_1": (1, 0, 1, 0, 2),  # 2 devices — keep
        "pay_2": (1, 0, 1, 0, 2),
    }
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=["/proc/payments/pay_1.json", "/proc/payments/pay_2.json"],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert res.dropped == []


def test_filter_drops_single_device_customer():
    """v0.1.74 t40 FP pattern: cust_025 uses 1 device across 3
    rapid cross-store purchases — legitimate, not fraud."""
    rows = {
        "pay_fraud":   (1, 0, 1, 0, 2),  # multi-device — keep
        "pay_legit_1": (1, 0, 1, 0, 1),  # single-device — drop
        "pay_legit_2": (1, 0, 1, 0, 1),
        "pay_legit_3": (1, 0, 1, 0, 1),
    }
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_fraud.json",
            "/proc/payments/pay_legit_1.json",
            "/proc/payments/pay_legit_2.json",
            "/proc/payments/pay_legit_3.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_fraud.json" in res.refs
    assert "/proc/payments/pay_legit_1.json" in res.dropped
    assert "/proc/payments/pay_legit_2.json" in res.dropped
    assert "/proc/payments/pay_legit_3.json" in res.dropped


def test_filter_threshold_override():
    """min_cust_devices=3 raises the bar."""
    rows = {
        "pay_1": (1, 0, 1, 0, 2),  # 2 devices — drops at threshold 3
        "pay_2": (1, 0, 1, 0, 3),  # 3 devices — keep
    }
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=["/proc/payments/pay_1.json", "/proc/payments/pay_2.json"],
        run_sql=lambda sql: _sql_output(rows),
        min_cust_devices=3,
    )
    assert "/proc/payments/pay_1.json" in res.dropped
    assert "/proc/payments/pay_2.json" in res.refs


def test_filter_t40_v074_repro():
    """Exact v0.1.74 t40 PROD scenario: 22 fraud rows (cust_068
    12, cust_031..035 2 each) + 3 FP rows (cust_025 single-device).
    All 25 match time-impossible. Filter must drop exactly the 3."""
    rows: dict[str, tuple[int, int, int, int, int]] = {}
    # cust_068 — 12 rows, 2 devices
    for i in range(12):
        rows[f"pay_068_{i}"] = (1, 0, 1, 0, 2)
    # cust_031..035 — 2 rows each, 2 devices
    for c in (31, 32, 33, 34, 35):
        for i in range(2):
            rows[f"pay_{c}_{i}"] = (1, 0, 1, 0, 2)
    # cust_025 — 3 rows, 1 device (FP)
    for i in range(3):
        rows[f"pay_025_{i}"] = (1, 0, 1, 0, 1)

    refs = [f"/proc/payments/{pid}.json" for pid in rows]
    res = filter_fraud_refs(
        task_text="confirmed fraud incident; identify the payment records",
        refs=refs,
        run_sql=lambda sql: _sql_output(rows),
    )
    assert len([p for p in res.refs if "/payments/" in p]) == 22
    assert len(res.dropped) == 3
    for i in range(3):
        assert f"/proc/payments/pay_025_{i}.json" in res.dropped


def test_filter_keeps_non_payment_refs():
    rows = {"pay_1": (1, 0, 1, 0, 2), "pay_2": (1, 0, 1, 0, 2)}
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/AGENTS.MD",
            "/docs/security.md",
            "/proc/payments/pay_1.json",
            "/proc/payments/pay_2.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/AGENTS.MD" in res.refs
    assert "/docs/security.md" in res.refs


def test_filter_passthrough_under_two_payments():
    res = filter_fraud_refs(
        task_text="fraud",
        refs=["/proc/payments/pay_1.json", "/AGENTS.MD"],
        run_sql=lambda sql: pytest.fail("should not be called"),
    )
    assert res.refs == ["/proc/payments/pay_1.json", "/AGENTS.MD"]


def test_filter_abstains_when_sql_fails():
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_1.json",
            "/proc/payments/pay_2.json",
            "/proc/payments/pay_3.json",
        ],
        run_sql=lambda sql: None,
    )
    assert res.aborted is True
    assert res.dropped == []


def test_filter_abstains_when_pid_missing_from_sql():
    rows = {"pay_1": (1, 0, 1, 0, 2), "pay_2": (1, 0, 1, 0, 2)}
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_1.json",
            "/proc/payments/pay_2.json",
            "/proc/payments/pay_unknown.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_unknown.json" in res.refs


def test_filter_sql_includes_device_count():
    captured = {}

    def run_sql(sql):
        captured["sql"] = sql
        return _sql_output({"pay_1": (1, 0, 1, 0, 2), "pay_2": (1, 0, 1, 0, 2)})

    filter_fraud_refs(
        task_text="fraud incident",
        refs=["/proc/payments/pay_1.json", "/proc/payments/pay_2.json"],
        run_sql=run_sql,
    )
    sql = captured["sql"]
    assert "cust_device_count" in sql
    assert "COUNT(DISTINCT ap2.device_fingerprint)" in sql
    assert "basket_archived = 1" in sql
    # Scoped to time-cluster (p4) only — not all-time archived history.
    assert "id IN (SELECT id FROM p4)" in sql


def test_looks_like_fraud_task():
    assert looks_like_fraud_task("confirmed fraud incident in archived payment history")
    assert looks_like_fraud_task("identify the fraudulent payment records")
    assert not looks_like_fraud_task("recover the 3DS flow for payment pay_001")
    assert not looks_like_fraud_task("how many wood screws")
