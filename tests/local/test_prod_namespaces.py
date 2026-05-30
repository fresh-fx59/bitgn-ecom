"""Tests for BITGN_LOCAL_PROD_PATHS — faithful PROD /proc namespace synth.

PROD serves records under namespaced paths (`/proc/payment-ledger/`,
`/proc/carts/`, `/proc/locations/`, `/proc/staff/`,
`/proc/return-workflows/`) per its AGENTS.MD, whereas the dev snapshots
store `record_path` as the flat dev paths (`/proc/payments/`,
`/proc/baskets/`, ...). The `BITGN_LOCAL_PROD_PATHS=1` mode lets
LocalEcomClient answer the PROD-namespaced reads so prod fixes can be
A/B'd faithfully against existing snapshots. Mirrors the
`BITGN_LOCAL_SQL_UNAVAILABLE` mode (commit 6d18051).

The synth resolves by the trailing id token against the table key
column (the `record_path` lookup misses because the db stores dev
paths), so the mode is path-agnostic.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bitgn_contest_agent.local.ecom_client import LocalEcomClient


def _req(**kw):
    return SimpleNamespace(**kw)


def _build_prod_schema_db(target: Path) -> None:
    """Minimal PROD-faithful catalogue.db with the namespaced tables.
    record_path columns use the DEV flat prefixes (as real snapshots
    do) so the test proves the token-fallback bridges to PROD paths."""
    conn = sqlite3.connect(str(target))
    try:
        conn.execute(
            "CREATE TABLE payment_transactions ("
            "payment_id TEXT, record_path TEXT, basket_id TEXT, "
            "payment_status TEXT, three_ds_status TEXT)"
        )
        conn.execute(
            "INSERT INTO payment_transactions VALUES "
            "('pay_001', '/proc/payments/pay_001.json', 'basket_001', "
            "'pending', 'recovery_required')"
        )
        conn.execute(
            "CREATE TABLE shopping_baskets ("
            "basket_id TEXT, record_path TEXT, customer_id TEXT, "
            "basket_status TEXT)"
        )
        conn.execute(
            "INSERT INTO shopping_baskets VALUES "
            "('basket_001', '/proc/baskets/basket_001.json', 'cust_001', "
            "'checked_out')"
        )
        conn.execute(
            "CREATE TABLE stores ("
            "store_id TEXT, record_path TEXT, store_name TEXT, city TEXT)"
        )
        conn.execute(
            "INSERT INTO stores VALUES "
            "('store_graz_lend', '/proc/stores/store_graz_lend.json', "
            "'Graz Lend', 'Graz')"
        )
        conn.execute(
            "CREATE TABLE employee_accounts ("
            "employee_id TEXT, record_path TEXT, employee_display_name TEXT, "
            "store_id TEXT)"
        )
        conn.execute(
            "INSERT INTO employee_accounts VALUES "
            "('emp_016', '/proc/employees/emp_016.json', 'Jane Doe', "
            "'store_graz_lend')"
        )
        conn.execute(
            "CREATE TABLE return_requests ("
            "return_id TEXT, record_path TEXT, basket_id TEXT, "
            "return_status TEXT)"
        )
        conn.execute(
            "INSERT INTO return_requests VALUES "
            "('ret_001', '/proc/returns/ret_001.json', 'basket_001', "
            "'approved')"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def prod_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "prod_ws"
    ws.mkdir()
    _build_prod_schema_db(ws / "catalogue.db")
    return ws


@pytest.fixture
def client(prod_workspace: Path) -> LocalEcomClient:
    return LocalEcomClient(prod_workspace)


def test_prod_payment_ledger_path_synthesizes(client, monkeypatch):
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/payment-ledger/pay_001.json"))
    assert r.content_type == "application/json"
    assert r.content and "pay_001" in r.content
    assert "recovery_required" in r.content


def test_prod_carts_path_synthesizes(client, monkeypatch):
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/carts/basket_001.json"))
    assert r.content and "basket_001" in r.content


def test_prod_locations_path_synthesizes(client, monkeypatch):
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/locations/store_graz_lend.json"))
    assert r.content and "store_graz_lend" in r.content


def test_prod_locations_nested_city_path_synthesizes(client, monkeypatch):
    # PROD may nest stores under a city segment; the trailing id token
    # still resolves the row.
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/locations/graz/store_graz_lend.json"))
    assert r.content and "store_graz_lend" in r.content


def test_prod_staff_path_synthesizes(client, monkeypatch):
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/staff/emp_016.json"))
    assert r.content and "emp_016" in r.content


def test_prod_return_workflows_path_synthesizes(client, monkeypatch):
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/return-workflows/ret_001.json"))
    assert r.content and "ret_001" in r.content


def test_prod_paths_404_when_flag_off(client, monkeypatch):
    monkeypatch.delenv("BITGN_LOCAL_PROD_PATHS", raising=False)
    with pytest.raises(FileNotFoundError):
        client.read(_req(path="/proc/payment-ledger/pay_001.json"))


def test_dev_payment_path_still_works_with_flag_on(client, monkeypatch):
    # dev paths must remain unconditional (flag-on must not break dev)
    monkeypatch.setenv("BITGN_LOCAL_PROD_PATHS", "1")
    r = client.read(_req(path="/proc/payments/pay_001.json"))
    assert r.content and "pay_001" in r.content
