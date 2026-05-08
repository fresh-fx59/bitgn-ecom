# tests/scraper/test_validate.py
"""Phase 3 validator tests."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from bitgn_scraper.db import init_schema
from bitgn_scraper.validate import (
    check_workspace_integrity,
    check_probe_coverage,
)


def _seed_one(db_path: Path, task_id: str, inst_hash: str, rule_count: int) -> None:
    with sqlite3.connect(db_path) as cx:
        cx.execute("PRAGMA foreign_keys = ON")
        cx.execute("""
            INSERT INTO task_instantiations
            (task_id, instantiation_hash, instruction, instruction_hash,
             tree_fingerprint, context_time, context_unix, benchmark_id,
             scraped_at, workspace_dir, workspace_byte_total, workspace_file_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, inst_hash, "instr", "h", "tfp", "t", 0,
              "bitgn/pac1-prod", "s", f"{task_id}/{inst_hash[:12]}", 0, 0))
        for i in range(rule_count):
            cx.execute("""
                INSERT INTO scoring_rules
                (task_id, instantiation_hash, rule_kind, rule_value,
                 confidence, derived_from, notes)
                VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """, (task_id, inst_hash, "expected_answer", f"v{i}", "high"))
        cx.commit()


def test_integrity_clean_when_files_match(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    inst_hash = "a" * 64
    _seed_one(db_path, "t001", inst_hash, rule_count=0)

    ws = tmp_path / "workspaces" / "t001" / inst_hash[:12]
    ws.mkdir(parents=True)
    (ws / "file.md").write_text("alpha", encoding="utf-8")
    sha = hashlib.sha256(b"alpha").hexdigest()
    with sqlite3.connect(db_path) as cx:
        cx.execute("""
            INSERT INTO workspace_files
            (task_id, instantiation_hash, path, is_dir, byte_size, sha256)
            VALUES (?, ?, ?, 0, 5, ?)
        """, ("t001", inst_hash, "/file.md", sha))
        cx.commit()

    report = check_workspace_integrity(db_path, tmp_path / "workspaces")
    assert report.checked == 1
    assert report.mismatches == []


def test_integrity_flags_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    inst_hash = "b" * 64
    _seed_one(db_path, "t002", inst_hash, rule_count=0)
    ws = tmp_path / "workspaces" / "t002" / inst_hash[:12]
    ws.mkdir(parents=True)
    (ws / "file.md").write_text("alpha", encoding="utf-8")
    with sqlite3.connect(db_path) as cx:
        cx.execute("""
            INSERT INTO workspace_files
            (task_id, instantiation_hash, path, is_dir, byte_size, sha256)
            VALUES (?, ?, ?, 0, 5, ?)
        """, ("t002", inst_hash, "/file.md", "deadbeef" * 8))
        cx.commit()

    report = check_workspace_integrity(db_path, tmp_path / "workspaces")
    assert report.checked == 1
    assert len(report.mismatches) == 1
    assert report.mismatches[0].path == "/file.md"


def test_coverage_buckets_tasks_by_rule_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_schema(db_path)
    _seed_one(db_path, "t001", "a" * 64, rule_count=2)
    _seed_one(db_path, "t002", "b" * 64, rule_count=0)
    report = check_probe_coverage(db_path)
    assert report.tasks_with_high_confidence == 1
    assert report.tasks_with_no_rules == 1
