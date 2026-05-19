"""Tests for fraud_cluster_filter (v0.1.74 id-share AND time-cluster)."""
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


# ── cluster builder (legacy helper) ──────────────────────────────────


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


# ── id-share AND time-cluster filter (current API) ───────────────────


def _sql_output(rows: dict[str, tuple[int, int, int, int]]) -> str:
    """Format: '<pid>|<n_total>|<n_id_share>|<in_time>|<in_coord>'."""
    return "\n".join(
        f"{pid}|{n_total}|{n_id_share}|{in_time}|{in_coord}"
        for pid, (n_total, n_id_share, in_time, in_coord) in rows.items()
    ) + "\n"


def test_filter_keeps_id_share_and_time_cluster():
    rows = {"pay_001": (4, 2, 1, 1)}  # all signals
    res = filter_fraud_refs(
        task_text="fraud incident identify the payments",
        refs=["/proc/payments/pay_001.json", "/proc/payments/pay_002.json"],
        run_sql=lambda sql: _sql_output(
            {"pay_001": (4, 2, 1, 1), "pay_002": (4, 2, 1, 1)}
        ),
    )
    assert res.dropped == []


def test_filter_drops_id_share_without_time_cluster():
    """v0.1.74 t40 FP pattern: row matches identity-share (shares
    fraudster's card/device) but is NOT in the time cluster
    (e.g., setup payment outside the burst window). Drop."""
    rows = {
        "pay_main_1": (4, 2, 1, 1),  # real fraud
        "pay_main_2": (4, 2, 1, 1),  # real fraud
        "pay_setup":  (1, 1, 0, 0),  # identity-share only, no time-cluster
    }
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_main_1.json",
            "/proc/payments/pay_main_2.json",
            "/proc/payments/pay_setup.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_main_1.json" in res.refs
    assert "/proc/payments/pay_main_2.json" in res.refs
    assert "/proc/payments/pay_setup.json" in res.dropped


def test_filter_drops_time_cluster_without_id_share():
    """Co-location-only: time-cluster + coord but no identity-share —
    legitimate small purchase caught in fraud burst's time window."""
    rows = {
        "pay_main": (4, 2, 1, 1),
        "pay_legit": (2, 0, 1, 1),  # co-location only
    }
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=[
            "/proc/payments/pay_main.json",
            "/proc/payments/pay_legit.json",
        ],
        run_sql=lambda sql: _sql_output(rows),
    )
    assert "/proc/payments/pay_legit.json" in res.dropped


def test_filter_can_disable_time_cluster_requirement():
    rows = {"pay_x": (1, 1, 0, 0)}
    # With time-cluster requirement: drop
    res = filter_fraud_refs(
        task_text="fraud incident",
        refs=["/proc/payments/pay_x.json", "/proc/payments/pay_y.json"],
        run_sql=lambda sql: _sql_output(
            {"pay_x": (1, 1, 0, 0), "pay_y": (1, 1, 0, 0)}
        ),
    )
    assert "/proc/payments/pay_x.json" in res.dropped
    # Without time-cluster requirement: keep
    res2 = filter_fraud_refs(
        task_text="fraud incident",
        refs=["/proc/payments/pay_x.json", "/proc/payments/pay_y.json"],
        run_sql=lambda sql: _sql_output(
            {"pay_x": (1, 1, 0, 0), "pay_y": (1, 1, 0, 0)}
        ),
        require_time_cluster=False,
    )
    assert "/proc/payments/pay_x.json" in res2.refs


def test_filter_keeps_non_payment_refs():
    rows = {"pay_001": (4, 2, 1, 1), "pay_002": (4, 2, 1, 1)}
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


def test_filter_abstains_when_pid_missing_from_sql():
    rows = {"pay_001": (4, 2, 1, 1), "pay_002": (4, 2, 1, 1)}
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


def test_filter_sql_shape_correct():
    captured = {}

    def run_sql(sql):
        captured["sql"] = sql
        return _sql_output(
            {"pay_001": (4, 2, 1, 1), "pay_002": (4, 2, 1, 1)}
        )

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
    assert "basket_archived = 1" in sql
    assert "n_id_share" in sql
    assert "in_time_cluster" in sql


def test_looks_like_fraud_task():
    assert looks_like_fraud_task(
        "We have a confirmed fraud incident in archived payment history."
    )
    assert not looks_like_fraud_task(
        "Recover the 3DS flow for payment pay_001"
    )
    assert not looks_like_fraud_task("How many products are wood screws?")
