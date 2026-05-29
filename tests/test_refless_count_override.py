"""Faithful local validation of the refless count override.

Reconstructs an in-memory SQLite catalogue from the extracted PROD-schema
snapshots (``artifacts/ws_snapshots/<task>_real2/sql/*.json`` +
``sql_schema.sql``) and runs the override's EXACT SQL against it, mimicking
the ``/bin/sql`` pipe-separated output. This is the local harness for the
refless count_per_store family — see memory
``project_ecom_count_completer_dead_in_prod`` (REFLESS COUNT TASKS section).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bitgn_contest_agent.refless_count_override import compute_refless_count

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "artifacts" / "ws_snapshots"


def _build_db(snap_dir: Path) -> sqlite3.Connection:
    schema = (snap_dir / "sql_schema.sql").read_text()
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    for jf in (snap_dir / "sql").glob("*.json"):
        table = jf.stem
        rows = json.loads(jf.read_text())
        if not rows:
            continue
        cols = list(rows[0].keys())
        placeholders = ",".join("?" for _ in cols)
        collist = ",".join(f'"{c}"' for c in cols)
        conn.executemany(
            f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})',
            [tuple(r.get(c) for c in cols) for r in rows],
        )
    conn.commit()
    return conn


def _make_run_sql(conn: sqlite3.Connection):
    def run_sql(sql: str) -> str | None:
        try:
            cur = conn.execute(sql)
        except sqlite3.Error:
            return None
        rows = cur.fetchall()
        # mimic /bin/sql pipe-separated output (header + rows)
        header = "|".join(d[0] for d in cur.description) if cur.description else ""
        lines = [header]
        for r in rows:
            lines.append("|".join("" if c is None else str(c) for c in r))
        return "\n".join(lines)

    return run_sql


def _instruction(snap_dir: Path) -> str:
    meta = json.loads((snap_dir / "run_0" / "metadata.json").read_text())
    return meta["instruction"]


# (snapshot, expected count) — both are refless count_per_store tasks with
# faithful PROD-schema extractions. t45 uses "fewer than 4" (<4), t16 uses
# "at least 1" (>=1): opposite directions, exercising threshold parsing.
EXACT_CASES = [
    ("t45_real2", 4),
    ("t16_real2", 3),
]


@pytest.mark.parametrize("snap,expected", EXACT_CASES)
def test_refless_count_exact(snap, expected):
    snap_dir = SNAP / snap
    if not (snap_dir / "sql_schema.sql").exists():
        pytest.skip(f"no faithful snapshot for {snap}")
    conn = _build_db(snap_dir)
    run_sql = _make_run_sql(conn)
    got = compute_refless_count(run_sql, _instruction(snap_dir))
    assert got == expected, f"{snap}: got {got}, expected {expected}"


def test_abstains_on_unparseable_threshold():
    # No comparison phrase → abstain.
    snap_dir = SNAP / "t45_real2"
    if not (snap_dir / "sql_schema.sql").exists():
        pytest.skip("no faithful snapshot")
    conn = _build_db(snap_dir)
    run_sql = _make_run_sql(conn)
    text = _instruction(snap_dir).replace("fewer than 4", "some")
    assert compute_refless_count(run_sql, text) is None


def test_abstains_on_two_thresholds():
    # Two comparisons → ambiguous direction → abstain.
    snap_dir = SNAP / "t45_real2"
    if not (snap_dir / "sql_schema.sql").exists():
        pytest.skip("no faithful snapshot")
    conn = _build_db(snap_dir)
    run_sql = _make_run_sql(conn)
    text = _instruction(snap_dir).replace(
        "fewer than 4", "fewer than 4 but at least 1"
    )
    assert compute_refless_count(run_sql, text) is None


def test_abstains_on_unknown_store():
    snap_dir = SNAP / "t45_real2"
    if not (snap_dir / "sql_schema.sql").exists():
        pytest.skip("no faithful snapshot")
    conn = _build_db(snap_dir)
    run_sql = _make_run_sql(conn)
    text = _instruction(snap_dir).replace(
        "Wilten PowerTool store in Innsbruck",
        "Nowhere PowerTool store in Atlantis",
    )
    assert compute_refless_count(run_sql, text) is None


def test_abstains_on_empty_db():
    # Empty/missing tables → SQL returns nothing → abstain, never crash.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE stores (store_id TEXT, store_name TEXT, city TEXT)")
    run_sql = _make_run_sql(conn)
    text = (
        "How many of these products have fewer than 4 items available in "
        "the Wilten PowerTool store in Innsbruck today: the Chainsaw from "
        "Fiskars in the Fiskars X 1CD-A3X Chainsaw line that has power "
        'source battery? Answer in exactly format "<COUNT:%d>"'
    )
    assert compute_refless_count(run_sql, text) is None
