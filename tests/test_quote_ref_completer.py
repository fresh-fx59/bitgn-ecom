"""Validate the quote ref completer against the faithful t47 snapshot.

Builds an in-memory SQLite from the extracted PROD-schema snapshot and runs
the completer's exact SQL — the local oracle for the t47 under-matching
failure (see memory project_ecom_t47_tsv_oracle).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bitgn_contest_agent.quote_ref_completer import (
    complete_quote_refs,
    looks_like_quote_task,
    resolve_matched_refs,
)

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "artifacts" / "ws_snapshots" / "t47_real2"

# The 4 exact-match SKUs the agent must reference (3 of which it under-matched).
EXPECTED_REFS = {
    "/proc/catalog/power_tools/corded_angle_grinder/PWR-1ALYVIXX.json",
    "/proc/catalog/paints_finishes/wood_stain_oil/PNT-1CJQ9WWP.json",
    "/proc/catalog/fasteners/wood_drywall_screws/FST-1GZ71C60.json",
    "/proc/catalog/cleaning/cloths_mops_wipes/CLN-3066WM0S.json",
}


def _build_db(snap_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript((snap_dir / "sql_schema.sql").read_text())
    for jf in (snap_dir / "sql").glob("*.json"):
        rows = json.loads(jf.read_text())
        if not rows:
            continue
        cols = list(rows[0].keys())
        conn.executemany(
            f'INSERT INTO "{jf.stem}" ({",".join(chr(34)+c+chr(34) for c in cols)}) '
            f'VALUES ({",".join("?" for _ in cols)})',
            [tuple(r.get(c) for c in cols) for r in rows],
        )
    conn.commit()
    return conn


def _run_sql(conn):
    def run_sql(sql: str):
        try:
            cur = conn.execute(sql)
        except sqlite3.Error:
            return None
        rows = cur.fetchall()
        header = "|".join(d[0] for d in cur.description) if cur.description else ""
        return "\n".join([header] + ["|".join("" if c is None else str(c) for c in r) for r in rows])
    return run_sql


def _instruction() -> str:
    return json.loads((SNAP / "run_0" / "metadata.json").read_text())["instruction"]


def _skip_if_missing():
    if not (SNAP / "sql_schema.sql").exists():
        pytest.skip("no faithful t47 snapshot")


def test_detects_quote_task():
    _skip_if_missing()
    assert looks_like_quote_task(_instruction())
    assert not looks_like_quote_task("How many products are available in Brno?")


def test_resolves_all_four_exact_matches():
    _skip_if_missing()
    refs = set(resolve_matched_refs(_run_sql(_build_db(SNAP)), _instruction()))
    assert EXPECTED_REFS <= refs, f"missing: {EXPECTED_REFS - refs}; got {refs}"


def test_completer_adds_the_three_under_matched_refs():
    _skip_if_missing()
    # Simulate the agent's actual under-match: it cited only PWR-1ALYVIXX.
    existing = ["/proc/catalog/power_tools/corded_angle_grinder/PWR-1ALYVIXX.json"]
    added = complete_quote_refs(_run_sql(_build_db(SNAP)), _instruction(), existing)
    # adds exactly the 3 it dropped, none already present
    assert set(added) == EXPECTED_REFS - set(existing)
    assert all(a not in existing for a in added)


def test_noop_on_non_quote_task():
    _skip_if_missing()
    added = complete_quote_refs(_run_sql(_build_db(SNAP)), "How many in Brno?", [])
    assert added == []
