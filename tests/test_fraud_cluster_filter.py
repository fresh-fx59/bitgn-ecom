"""Tests for fraud_cluster_filter (v0.1.72 multi-pattern signal)."""
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


# ── cluster builder (legacy single-pattern, kept for completeness) ───


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


# ── multi-pattern filter (current API) ───────────────────────────────


def _multi_pattern_sql_output(rows: dict[str, int]) -> str:
    """Format rows the way `/bin/sql` returns: '<pid>|<n_patterns>' lines."""
    lines = []
    for pid, n in rows.items():
        lines.append(f"{pid}|{n}")
    return "\n".join(lines) + "\n"


def test_filter_drops_single_pattern_rows():
    """v0.1.66+ t40 FP pattern: rows that only match the
    time-impossible cluster (1 pattern) drop, while rows in
    multiple patterns (2+) stay."""
    rows = {
        "pay_001": 3,  # 3 patterns — keep
        "pay_002": 4,  # 4 patterns — keep
        "pay_003": 1,  # only 1 pattern — drop (legitimate caught in burst)
    }
    res = filter_fraud_refs(
        task_text="confirmed fraud incident; identify the payment records",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
            "/proc/payments/pay_003.json",
        ],
        run_sql=lambda sql: _multi_pattern_sql_output(rows),
    )
    assert "/proc/payments/pay_001.json" in res.refs
    assert "/proc/payments/pay_002.json" in res.refs
    assert "/proc/payments/pay_003.json" in res.dropped


def test_filter_threshold_2_default():
    """Default min_patterns=2 keeps n=2 and drops n=1."""
    rows = {"pay_001": 2, "pay_002": 1}
    res = filter_fraud_refs(
        task_text="fraud incident identify",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=lambda sql: _multi_pattern_sql_output(rows),
    )
    assert "/proc/payments/pay_001.json" in res.refs
    assert "/proc/payments/pay_002.json" in res.dropped


def test_filter_threshold_override():
    """min_patterns=3 raises the bar."""
    rows = {"pay_001": 2, "pay_002": 3}
    res = filter_fraud_refs(
        task_text="fraud incident identify",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=lambda sql: _multi_pattern_sql_output(rows),
        min_patterns=3,
    )
    assert "/proc/payments/pay_001.json" in res.dropped
    assert "/proc/payments/pay_002.json" in res.refs


def test_filter_keeps_non_payment_refs():
    rows = {"pay_001": 3, "pay_002": 3}
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/AGENTS.MD",
            "/docs/security.md",
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=lambda sql: _multi_pattern_sql_output(rows),
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
    """SQL returned signal counts for some pids but not all — leave
    the unknown ones alone (e.g., archived payments not in the live
    table)."""
    rows = {"pay_001": 3, "pay_002": 3}
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
            "/proc/payments/pay_unknown.json",
        ],
        run_sql=lambda sql: _multi_pattern_sql_output(rows),
    )
    assert "/proc/payments/pay_unknown.json" in res.refs
    assert res.dropped == []


def test_filter_sql_query_uses_id_column():
    """The fix from v0.1.71 — query column is 'id', not 'pay_id'."""
    captured = {}

    def run_sql(sql):
        captured["sql"] = sql
        return _multi_pattern_sql_output({"pay_001": 3, "pay_002": 3})

    filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_001.json",
            "/proc/payments/pay_002.json",
        ],
        run_sql=run_sql,
    )
    sql = captured["sql"]
    # Multi-pattern query selects 'id' from the payments table.
    assert "id IN" in sql
    assert "pay_id IN" not in sql
    # And uses the archived-payment scope.
    assert "basket_archived = 1" in sql


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
