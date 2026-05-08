"""Schema + helper tests for src/bitgn_scraper/db.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bitgn_scraper.db import (
    EXPECTED_TABLES,
    connect,
    init_schema,
    insert_scoring_rule,
)


def test_init_schema_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r[0] for r in rows}
    for table in EXPECTED_TABLES:
        assert table in names, f"missing table {table!r}; got {names!r}"


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    init_schema(db_path)  # should not raise
    with connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM scoring_rules").fetchone()[0]
    assert n == 0


def test_insert_scoring_rule_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    rule_id = insert_scoring_rule(
        db_path,
        task_id="t000",
        instantiation_hash="abc123",
        rule_kind="expected_answer",
        rule_value="1989-02-16",
        confidence="high",
        derived_from=None,
        notes="seeded from cf90740 outcome trace",
    )
    assert rule_id > 0
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT task_id, rule_kind, rule_value, confidence, notes "
            "FROM scoring_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
    assert row == (
        "t000",
        "expected_answer",
        "1989-02-16",
        "high",
        "seeded from cf90740 outcome trace",
    )


def test_insert_scoring_rule_rejects_bad_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    with pytest.raises(ValueError, match="confidence must be"):
        insert_scoring_rule(
            db_path,
            task_id="t000",
            instantiation_hash="abc",
            rule_kind="expected_answer",
            rule_value="x",
            confidence="bogus",
            derived_from=None,
            notes=None,
        )


def test_foreign_key_enforcement(tmp_path: Path) -> None:
    """Verify PRAGMA foreign_keys = ON actually rejects orphaned rows."""
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    with connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workspace_files "
                "(task_id, instantiation_hash, path, is_dir, byte_size, sha256) "
                "VALUES ('t000', 'no-such-hash', '/AGENTS.MD', 0, 100, 'deadbeef')"
            )
            conn.commit()
