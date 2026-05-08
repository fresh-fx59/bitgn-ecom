# src/bitgn_scraper/validate.py
"""Phase 3 validators — integrity, coverage, determinism."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class IntegrityMismatch:
    task_id: str
    instantiation_hash: str
    path: str
    expected_sha256: str
    actual_sha256: str


@dataclass(frozen=True)
class IntegrityReport:
    checked: int
    mismatches: list[IntegrityMismatch] = field(default_factory=list)
    missing_files: list[tuple[str, str, str]] = field(default_factory=list)


def check_workspace_integrity(
    db_path: Path, workspace_root: Path
) -> IntegrityReport:
    """Re-hash on-disk files, compare to workspace_files.sha256."""
    mismatches: list[IntegrityMismatch] = []
    missing: list[tuple[str, str, str]] = []
    checked = 0
    with sqlite3.connect(db_path) as cx:
        rows = cx.execute("""
            SELECT wf.task_id, wf.instantiation_hash, wf.path, wf.sha256, ti.workspace_dir
            FROM workspace_files wf
            JOIN task_instantiations ti
              ON ti.task_id = wf.task_id AND ti.instantiation_hash = wf.instantiation_hash
            WHERE wf.is_dir = 0
        """).fetchall()
    for task_id, inst, path, expected, ws_rel in rows:
        if expected == "READ_ERROR":
            continue
        rel = path.lstrip("/")
        if not rel:
            continue
        on_disk = workspace_root / ws_rel / rel
        if not on_disk.exists():
            missing.append((task_id, inst, path))
            continue
        actual = hashlib.sha256(on_disk.read_bytes()).hexdigest()
        checked += 1
        if actual != expected:
            mismatches.append(IntegrityMismatch(
                task_id=task_id, instantiation_hash=inst, path=path,
                expected_sha256=expected, actual_sha256=actual,
            ))
    return IntegrityReport(
        checked=checked, mismatches=mismatches, missing_files=missing,
    )


@dataclass(frozen=True)
class CoverageReport:
    total_tasks: int
    tasks_with_high_confidence: int
    tasks_with_low_confidence_only: int
    tasks_with_no_rules: int


def check_probe_coverage(db_path: Path) -> CoverageReport:
    """Bucket each task by the highest-confidence rule it has."""
    with sqlite3.connect(db_path) as cx:
        all_tasks = {r[0] for r in cx.execute(
            "SELECT DISTINCT task_id FROM task_instantiations"
        )}
        high = {r[0] for r in cx.execute(
            "SELECT DISTINCT task_id FROM scoring_rules WHERE confidence = 'high'"
        )}
        any_rule = {r[0] for r in cx.execute(
            "SELECT DISTINCT task_id FROM scoring_rules"
        )}
    no_rules = all_tasks - any_rule
    low_only = any_rule - high
    return CoverageReport(
        total_tasks=len(all_tasks),
        tasks_with_high_confidence=len(high),
        tasks_with_low_confidence_only=len(low_only),
        tasks_with_no_rules=len(no_rules),
    )


@dataclass(frozen=True)
class DeterminismFinding:
    task_id: str
    stored_hashes: list[str]
    new_hashes: list[str]
    overlap: int
    fully_saturated: bool


@dataclass(frozen=True)
class DeterminismReport:
    findings: list[DeterminismFinding] = field(default_factory=list)


def check_determinism(
    *,
    db_path: Path,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_ids: list[str],
    benchmark_id: str = "bitgn/pac1-prod",
    n_samples_per_task: int = 5,
) -> DeterminismReport:
    """Re-scrape `n_samples_per_task` instantiations per task; compare hashes."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ContextRequest
    from bitgn_scraper.fingerprint import instantiation_hash
    from bitgn_scraper.workspace_walk import walk_workspace

    findings: list[DeterminismFinding] = []
    with sqlite3.connect(db_path) as cx:
        stored_by_task = {
            tid: [r[0] for r in cx.execute(
                "SELECT instantiation_hash FROM task_instantiations WHERE task_id = ?",
                (tid,),
            ).fetchall()]
            for tid in task_ids
        }

    for tid in task_ids:
        stored = stored_by_task.get(tid, [])
        new_hashes: list[str] = []
        for _ in range(n_samples_per_task):
            started = harness_client.start_playground(
                StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=tid)
            )
            try:
                pcm = pcm_factory(started.harness_url)
                _ = pcm.context(ContextRequest())
                files = walk_workspace(pcm)
                new_hashes.append(instantiation_hash(started.instruction, files))
            finally:
                harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))
        overlap = len(set(new_hashes) & set(stored))
        findings.append(DeterminismFinding(
            task_id=tid,
            stored_hashes=sorted(stored),
            new_hashes=sorted(new_hashes),
            overlap=overlap,
            fully_saturated=overlap == len(set(new_hashes)),
        ))
    return DeterminismReport(findings=findings)
