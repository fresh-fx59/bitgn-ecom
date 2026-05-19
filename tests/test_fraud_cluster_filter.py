"""Tests for fraud_cluster_filter (v0.1.73 identity-share gating)."""
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
    assert _parse_iso("") is None


# ── cluster builder (legacy single-pattern helper) ───────────────────


def test_cluster_singletons_not_in_cluster():
    rows = [
        PaymentRow("pay_1", "cust_A", "store_X", _t("2021-04-28T14:00:00Z")),
        PaymentRow("pay_2", "cust_B", "store_Y", _t("2021-04-28T14:00:00Z")),
    ]
    assert _build_cluster_membership(rows) == set()


def test_cluster_pair_same_customer_diff_store_within_window():
    rows = [
        PaymentRow("pay_1", "cust_A", "store_X", _t("2021-04-28T14:00:00Z")),
        PaymentRow("pay_2", "cust_A", "store_Y", _t("2021-04-28T14:05:00Z")),
    ]
    assert _build_cluster_membership(rows) == {"pay_1", "pay_2"}


def test_cluster_transitive_three_rows():
    rows = [
        PaymentRow("pay_1", "cust_A", "store_X", _t("2021-04-28T14:00:00Z")),
        PaymentRow("pay_2", "cust_A", "store_Y", _t("2021-04-28T14:05:00Z")),
        PaymentRow("pay_3", "cust_A", "store_Z", _t("2021-04-28T14:15:00Z")),
    ]
    assert _build_cluster_membership(rows) == {"pay_1", "pay_2", "pay_3"}


# ── identity-share filter (current API) ──────────────────────────────


def _sql_output(rows: dict[str, tuple[int, int]]) -> str:
    """Format rows the way `/bin/sql` returns:
    '<pid>|<n_total>|<n_id_share>' lines."""
    return "\n".join(
        f"{pid}|{n_total}|{n_id_share}"
        for pid, (n_total, n_id_share) in rows.items()
    ) + "\n"


def test_filter_drops_colocation_only_rows():
    """v0.1.72 t40 FP pattern: rows that only match P4+P5 (time-
    impossible + coord cluster) get dropped — they're legitimate
    small purchases caught in the fraud-burst time/coord window.
    Rows with at least one identity-share signal (card/device) stay."""
    rows = {
        "pay_001": (3, 1),  # 3 total, 1 id-share — KEEP
        "pay_002": (4, 2),  # 4 total, 2 id-share — KEEP
        "pay_003": (2, 0),  # 2 total (P4+P5), 0 id-share — DROP
    }
    res = filter_fraud_refs(
        task_text="confirmed fraud incident; identify the payment records",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
            "/proc/payments/pay_003.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_001.json" in res.refs
    assert "/proc/payments/pay_002.json" in res.refs
    assert "/proc/payments/pay_003.json" in res.dropped


def test_filter_keeps_single_id_share():
    """Just one identity-share signal is enough."""
    rows = {
        "pay_001": (1, 1),
        "pay_002": (1, 0),
    }
    res = filter_fraud_refs(
        task_text="fraud incident identify",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_001.json" in res.refs
    assert "/proc/payments/pay_002.json" in res.dropped


def test_filter_threshold_override():
    """min_id_share=2 requires two identity-share signals."""
    rows = {
        "pay_001": (3, 1),
        "pay_002": (3, 2),
    }
    res = filter_fraud_refs(
        task_text="fraud incident identify",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
        min_id_share=2,
    )
    assert "/proc/payments/pay_001.json" in res.dropped
    assert "/proc/payments/pay_002.json" in res.refs


def test_filter_keeps_non_payment_refs():
    rows = {"pay_001": (3, 1), "pay_002": (3, 1)}
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/AGENTS.MD",
            "/docs/security.md",
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/AGENTS.MD" in res.refs
    assert "/docs/security.md" in res.refs


def test_filter_passthrough_when_under_two_payments():
    res = filter_fraud_refs(
        task_text="fraud",
        refs=["/proc/payments/pay_001.json", "/AGENTS.MD"],
        run_sql=lambda sql: pytest.fail("should not be called"),
    )
    assert res.refs == ["/proc/payments/pay_001.json", "/AGENTS.MD"]
    assert res.dropped == []


def test_filter_abstains_when_sql_fails():
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
            "/proc/payments/pay_003.json",
        ],
        run_sql=lambda sql: None,
    )
    assert res.aborted is True
    assert res.dropped == []
    assert "/proc/payments/pay_003.json" in res.refs


def test_filter_abstains_when_pid_missing_from_sql():
    rows = {"pay_001": (3, 1), "pay_002": (3, 1)}
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
            "/proc/payments/pay_unknown.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_unknown.json" in res.refs
    assert res.dropped == []


def test_filter_sql_query_shape():
    """SQL queries `id` column from payments WHERE basket_archived=1
    and selects (id, n_patterns, n_id_share)."""
    captured = {}

    def run_sql(sql):
        captured["sql"] = sql
        return _sql_output({"pay_001": (3, 1), "pay_002": (3, 1)})

    filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=run_sql,
    )
    sql = captured["sql"]
    assert "id IN" in sql
    assert "pay_id IN" not in sql
    assert "basket_archived = 1" in sql
    assert "n_id_share" in sql


def test_looks_like_fraud_task():
    assert looks_like_fraud_task(
        "We have a confirmed fraud incident in archived payment history."
    )
    assert looks_like_fraud_task(
        "Identify the fraudulent payment records from history."
    )
    assert not looks_like_fraud_task(
        "Recover the 3DS flow for payment pay_001"
    )
    assert not looks_like_fraud_task("How many products are wood screws?")
