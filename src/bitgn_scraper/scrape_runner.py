# src/bitgn_scraper/scrape_runner.py
"""Phase 1 scrape orchestrator — per-task loop with saturation heuristic.

For each task_id:
  - Repeatedly StartPlayground, walk workspace, compute instantiation_hash.
  - Stop after `saturation_threshold` consecutive duplicates, or `max_attempts` total.
  - Persist new instantiations to SQLite + flat files; EndTrial after each call.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bitgn_scraper.fingerprint import FileRecord, instantiation_hash, tree_fingerprint
from bitgn_scraper.workspace_walk import walk_workspace


def scrape_task(
    *,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    db_path: Path,
    workspace_root: Path,
    max_attempts: int = 30,
    saturation_threshold: int = 5,
) -> int:
    """Scrape one task; return count of NEW instantiations persisted."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import ContextRequest

    seen: set[str] = _load_existing_hashes(db_path, task_id)
    new_count = 0
    consecutive_dupes = 0

    for attempt in range(max_attempts):
        started = harness_client.start_playground(
            StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=task_id)
        )
        try:
            pcm = pcm_factory(started.harness_url)
            ctx = pcm.context(ContextRequest())
            files = walk_workspace(pcm)
            inst_hash = instantiation_hash(started.instruction, files)

            if inst_hash in seen:
                consecutive_dupes += 1
                if consecutive_dupes >= saturation_threshold:
                    break
                continue

            seen.add(inst_hash)
            consecutive_dupes = 0
            new_count += 1
            _persist_instantiation(
                db_path=db_path,
                workspace_root=workspace_root,
                task_id=task_id,
                instantiation_hash_=inst_hash,
                instruction=started.instruction,
                ctx_time=ctx.time,
                ctx_unix=ctx.unix_time,
                benchmark_id=benchmark_id,
                files=files,
                pcm=pcm,
            )
        finally:
            harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    return new_count


def _load_existing_hashes(db_path: Path, task_id: str) -> set[str]:
    with sqlite3.connect(db_path) as cx:
        rows = cx.execute(
            "SELECT instantiation_hash FROM task_instantiations WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    return {r[0] for r in rows}


def _persist_instantiation(
    *,
    db_path: Path,
    workspace_root: Path,
    task_id: str,
    instantiation_hash_: str,
    instruction: str,
    ctx_time: str,
    ctx_unix: int,
    benchmark_id: str,
    files: list[FileRecord],
    pcm: Any,
) -> None:
    """Write the row, the workspace_files index, and the flat files."""
    from bitgn.vm.pcm_pb2 import ReadRequest

    instruction_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    tree_fp = tree_fingerprint(files)
    workspace_dir_rel = f"{task_id}/{instantiation_hash_[:12]}"
    workspace_dir_abs = workspace_root / workspace_dir_rel
    workspace_dir_abs.mkdir(parents=True, exist_ok=True)

    byte_total = sum(f.byte_size for f in files)
    file_count = sum(1 for f in files if f.path != "/")
    scraped_at = datetime.now(tz=timezone.utc).isoformat()

    # Dump _meta.json
    (workspace_dir_abs / "_meta.json").write_text(json.dumps({
        "task_id": task_id,
        "instantiation_hash": instantiation_hash_,
        "instruction": instruction,
        "context_time": ctx_time,
        "scraped_at": scraped_at,
    }, indent=2, sort_keys=True))

    # Re-Read each file (workspace_walk only kept hashes/sizes) and dump to disk
    for rec in files:
        if rec.sha256 == "READ_ERROR":
            continue
        rel = rec.path.lstrip("/")
        if not rel:
            continue
        out = workspace_dir_abs / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = pcm.read(ReadRequest(path=rec.path))
            out.write_text(resp.content, encoding="utf-8")
        except Exception:
            # Already recorded as READ_ERROR upstream; skip disk write.
            pass

    with sqlite3.connect(db_path) as cx:
        cx.execute("PRAGMA foreign_keys = ON")
        # context_time/context_unix snapshot the harness clock from the trial that
        # first observed this (instruction, workspace) pair; the hash itself excludes
        # them, so subsequent trials with different clocks collapse to this row.
        cx.execute(
            """
            INSERT INTO task_instantiations
            (task_id, instantiation_hash, instruction, instruction_hash,
             tree_fingerprint, context_time, context_unix, benchmark_id,
             scraped_at, workspace_dir, workspace_byte_total, workspace_file_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, instantiation_hash_, instruction, instruction_hash,
                tree_fp, ctx_time, ctx_unix, benchmark_id,
                scraped_at, workspace_dir_rel, byte_total, file_count,
            ),
        )
        cx.executemany(
            """
            INSERT INTO workspace_files
            (task_id, instantiation_hash, path, is_dir, byte_size, sha256)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (task_id, instantiation_hash_, f.path, 0, f.byte_size, f.sha256)
                for f in files
            ],
        )
        cx.commit()

