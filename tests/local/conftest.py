"""Per-package conftest for local-harness tests.

Builds the SQLite catalogue from a CSV at session start so the fixture
workspace is fully reproducible. Keeps binary `.db` files out of git
while still letting tests assert against deterministic SQL output.
"""
from __future__ import annotations

import csv
import shutil
import sqlite3
from pathlib import Path

import pytest


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "local_ecom"


def _build_catalogue(target: Path) -> None:
    """Drop a fresh catalogue.db at `target` populated from orders.csv
    plus a small hard-coded customers table. Idempotent — overwrites
    any existing file."""
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                total INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                country TEXT NOT NULL
            )
        """)
        with (_FIXTURE_DIR / "data" / "orders.csv").open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                conn.execute(
                    "INSERT INTO orders (id, customer_id, total, status) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        int(row["id"]), int(row["customer_id"]),
                        int(row["total"]), row["status"],
                    ),
                )
        conn.executemany(
            "INSERT INTO customers (id, name, country) VALUES (?, ?, ?)",
            [
                (10, "Acme Co", "DE"),
                (11, "Pimoroni Europe SARL", "FR"),
                (12, "Globex", "GB"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fixture_workspace(tmp_path: Path) -> Path:
    """Copy the on-disk fixture into a tmp_path and build a fresh
    catalogue.db inside it. Tests get an isolated, mutable workspace
    per call so write/delete assertions don't leak across tests."""
    dst = tmp_path / "ecom_ws"
    shutil.copytree(_FIXTURE_DIR, dst)
    _build_catalogue(dst / "catalogue.db")
    return dst
