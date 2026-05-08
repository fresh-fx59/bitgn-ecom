"""SQLite schema + thin typed accessors for the scraper store.

The schema mirrors the spec at
docs/superpowers/specs/2026-04-26-bitgn-local-harness-clone-design.md,
"Storage Layer". Each table has one responsibility:

  task_instantiations  one row per (task_id, instantiation_hash)
  workspace_files      one row per file inside an instantiation
  probe_log            append-only log of grader probes
  scoring_rules        parsed grader rules with provenance

This module never touches the network and never calls into other
scraper modules; it's the bottom of the import graph.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

EXPECTED_TABLES: tuple[str, ...] = (
    "task_instantiations",
    "workspace_files",
    "probe_log",
    "scoring_rules",
)

VALID_CONFIDENCE: frozenset[str] = frozenset({"high", "medium", "low"})

# NOTE: `derived_from` in `scoring_rules` is intentionally nullable.
# Seed rules mined from existing JSONL traces and server logs (Phase 1.5 tasks
# 4-7) predate any probe call, so there is no probe_log row to reference.
# Only rules produced by Phase-2 probes will have a non-NULL derived_from.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_instantiations (
    task_id              TEXT NOT NULL,
    instantiation_hash   TEXT NOT NULL,
    instruction          TEXT NOT NULL,
    instruction_hash     TEXT NOT NULL,
    tree_fingerprint     TEXT NOT NULL,
    context_time         TEXT NOT NULL,
    context_unix         INTEGER NOT NULL,
    benchmark_id         TEXT NOT NULL,
    scraped_at           TEXT NOT NULL,
    workspace_dir        TEXT NOT NULL,
    workspace_byte_total INTEGER NOT NULL,
    workspace_file_count INTEGER NOT NULL,
    PRIMARY KEY (task_id, instantiation_hash)
);

CREATE INDEX IF NOT EXISTS idx_task ON task_instantiations(task_id);

CREATE TABLE IF NOT EXISTS workspace_files (
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    path               TEXT NOT NULL,
    is_dir             INTEGER NOT NULL,
    byte_size          INTEGER NOT NULL,
    sha256             TEXT NOT NULL,
    PRIMARY KEY (task_id, instantiation_hash, path),
    FOREIGN KEY (task_id, instantiation_hash)
        REFERENCES task_instantiations(task_id, instantiation_hash)
);

CREATE TABLE IF NOT EXISTS probe_log (
    probe_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    probe_kind         TEXT NOT NULL,
    submitted_answer   TEXT,
    submitted_refs     TEXT,
    submitted_outcome  TEXT,
    submitted_writes   TEXT,
    score              REAL,
    score_detail_raw   TEXT,
    trial_id           TEXT,
    probed_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probe_task ON probe_log(task_id);

CREATE TABLE IF NOT EXISTS scoring_rules (
    rule_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    instantiation_hash TEXT NOT NULL,
    rule_kind          TEXT NOT NULL,
    rule_value         TEXT NOT NULL,
    confidence         TEXT NOT NULL,
    derived_from       INTEGER,
    notes              TEXT,
    FOREIGN KEY (derived_from) REFERENCES probe_log(probe_id)
);

CREATE INDEX IF NOT EXISTS idx_rules_task ON scoring_rules(task_id, instantiation_hash);
"""


def init_schema(db_path: str | Path) -> None:
    """Create all tables if they don't exist. Idempotent."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def insert_scoring_rule(
    db_path: str | Path,
    *,
    task_id: str,
    instantiation_hash: str,
    rule_kind: str,
    rule_value: str,
    confidence: str,
    derived_from: int | None,
    notes: str | None,
) -> int:
    """Insert one row into scoring_rules. Returns the new rule_id."""
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(
            f"confidence must be one of {sorted(VALID_CONFIDENCE)!r}, got {confidence!r}"
        )
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO scoring_rules "
            "(task_id, instantiation_hash, rule_kind, rule_value, "
            " confidence, derived_from, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, instantiation_hash, rule_kind, rule_value,
             confidence, derived_from, notes),
        )
        conn.commit()
        return int(cur.lastrowid)
