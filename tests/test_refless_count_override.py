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

import types

from bitgn_contest_agent.refless_count_override import (
    compute_refless_count,
    compute_refless_count_from_spec,
    looks_like_refless_count,
)


def _P(brand, model, series="", **attrs):
    o = types.SimpleNamespace()
    o.brand, o.model, o.series, o.attributes, o.name = brand, model, series, attrs, ""
    return o

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


def test_text_detector_fires_on_template_independent_of_kind():
    # The PROD bug: classifier left kind=None so the kind-gated override was
    # dead. The text detector must catch the canonical count template.
    for snap in ("t45_real2", "t16_real2"):
        snap_dir = SNAP / snap
        if not (snap_dir / "sql_schema.sql").exists():
            continue
        assert looks_like_refless_count(_instruction(snap_dir))
    assert not looks_like_refless_count("What is the birthday of customer X?")
    # template present but no parseable threshold → abstain (resolver would too)
    assert not looks_like_refless_count(
        "how many of these products are available in the Brno store"
    )


def test_spec_based_resolver_uses_llm_parse():
    # The PROD-robust path: consume task_spec.products + store_descriptor
    # (LLM adaptive parse) instead of regex-parsing raw text. resolve_store_id
    # handles varying store phrasings; exact attr matching avoids 500≈5000.
    if not (SNAP / "t45_real2" / "sql_schema.sql").exists():
        pytest.skip("no faithful snapshots")
    conn45 = _build_db(SNAP / "t45_real2")
    spec45 = types.SimpleNamespace(
        store_descriptor="Wilten PowerTool store in Innsbruck",
        products=[
            _P("Fiskars", "1CD-A3X", power_source="battery"),
            _P("Mobil", "1ZE-TCR", volume_ml="5000", viscosity="15W-40"),
            _P("Keter", "2OO-VJU", storage_type="parts case", color_family="Yellow", volume_l="8"),
            _P("Engelbert Strauss", "37H-N9K", color_family="Black"),
            _P("Sika", "28T-UV8", sealant_type="hybrid sealant", color_family="Gray", volume_ml="300"),
            _P("Sonax", "304-ZK0", length_mm="450"),
        ],
    )
    assert compute_refless_count_from_spec(spec45, _make_run_sql(conn45), "fewer than 4") == 4

    conn16 = _build_db(SNAP / "t16_real2")
    spec16 = types.SimpleNamespace(
        store_descriptor="Veveri PowerTool shop in Brno",
        products=[
            _P("Honeywell", "KJB-LZD", size="XL", color_family="Orange", protection_class="cut-3"),
            _P("Gorilla", "HL7-T2E", product_type="masking tape"),
            _P("Hager", "29D-PDT", color_family="White", length_m="2"),
            _P("Sonax", "300-EAF", length_mm="600"),
            _P("Mellerud", "BO7-35J", cleaner_type="glass cleaner", volume_ml="500"),
            _P("AlcaPlast", "33F-7U9", connector_type="shower hose", diameter_mm="12"),
        ],
    )
    assert compute_refless_count_from_spec(spec16, _make_run_sql(conn16), "at least 1") == 3


def test_spec_based_abstains_on_unresolvable_product():
    # A product whose attributes match no variant must ABSTAIN (return None),
    # NOT be silently treated as 0-available (which would falsely count it on
    # a "fewer than" task).
    if not (SNAP / "t45_real2" / "sql_schema.sql").exists():
        pytest.skip("no faithful snapshot")
    conn = _build_db(SNAP / "t45_real2")
    spec = types.SimpleNamespace(
        store_descriptor="Wilten PowerTool store in Innsbruck",
        products=[_P("Fiskars", "1CD-A3X", power_source="NONSENSE")],
    )
    assert compute_refless_count_from_spec(spec, _make_run_sql(conn), "fewer than 4") is None


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
