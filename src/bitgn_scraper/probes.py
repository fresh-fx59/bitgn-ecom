# src/bitgn_scraper/probes.py
"""Phase 2 probes — Strategy B (one trial per probe, post-hoc match by instruction_hash).

Probe order:
    P1_empty           — answer="", refs=[], writes={}, OUTCOME_OK
    P2_extracted       — answer=extracted_so_far, refs=[], writes={}, OUTCOME_OK
    P3_with_refs       — answer=extracted, refs=extracted, writes={}, OUTCOME_OK
    P4_with_writes     — answer=extracted, refs=extracted, writes=extracted, OUTCOME_OK
    P5_outcome_alt     — answer=extracted, refs=extracted, writes=extracted, OUTCOME_NONE_CLARIFICATION

Adaptive stopping: any probe returning score=1.0 ends the chain — the
remaining rules are already known.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bitgn_scraper.db import insert_scoring_rule
from bitgn_scraper.probe_extract import extract_probe_rules

logger = logging.getLogger(__name__)

_PROBE_ORDER = ["P1_empty", "P2_extracted", "P3_with_refs", "P4_with_writes", "P5_outcome_alt"]


def probe_instantiation(
    *,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instruction_hash: str,
    known_rules: dict[str, list[str]],
    db_path: Path,
    run_diagnostic_p2b: bool = False,
    run_diagnostic_p6: bool = False,
) -> int:
    """Run probes for one instantiation; return count of probes fired.

    Updates known_rules in-place so callers can pass pre-known rules in
    (e.g. from Phase 1.5 seed) and read the post-probe state out.
    """
    n_fired = 0
    for probe_kind in _PROBE_ORDER:
        ans, refs, writes_map, outcome = _build_probe(probe_kind, known_rules)

        probe_id, score, detail = _run_single_probe(
            harness_client=harness_client,
            pcm_factory=pcm_factory,
            task_id=task_id,
            benchmark_id=benchmark_id,
            instantiation_hash=instruction_hash,
            probe_kind=probe_kind,
            ans=ans,
            refs=refs,
            writes=writes_map,
            outcome=outcome,
            db_path=db_path,
        )
        n_fired += 1

        for rule in extract_probe_rules(" ".join(detail)):
            insert_scoring_rule(
                db_path=db_path,
                task_id=task_id,
                instantiation_hash=instruction_hash,
                rule_kind=rule.rule_kind,
                rule_value=rule.rule_value,
                confidence="high",
                derived_from=probe_id,
                notes=f"probe={probe_kind}",
            )
            known_rules.setdefault(rule.rule_kind, []).append(rule.rule_value)

        if score >= 1.0:
            break

    if run_diagnostic_p2b:
        n_fired += _run_p2b(harness_client, pcm_factory, task_id, benchmark_id,
                            instruction_hash, known_rules, db_path)
    if run_diagnostic_p6:
        n_fired += _run_p6(harness_client, pcm_factory, task_id, benchmark_id,
                           instruction_hash, db_path)
    return n_fired


def _run_single_probe(
    *,
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instantiation_hash: str,
    probe_kind: str,
    ans: str,
    refs: list[str],
    writes: dict[str, str],
    outcome: int,
    db_path: Path,
) -> tuple[int, float, list[str]]:
    """Run one probe trial and persist the row. Returns (probe_id, score, score_detail)."""
    from bitgn.harness_pb2 import EndTrialRequest, StartPlaygroundRequest
    from bitgn.vm.pcm_pb2 import AnswerRequest, WriteRequest
    from connectrpc.errors import ConnectError

    started = harness_client.start_playground(
        StartPlaygroundRequest(benchmark_id=benchmark_id, task_id=task_id)
    )
    try:
        pcm = pcm_factory(started.harness_url)
        for path, content in writes.items():
            pcm.write(WriteRequest(path=path, content=content))
        try:
            pcm.answer(AnswerRequest(message=ans, outcome=outcome))
        except ConnectError:
            logger.debug("pcm.answer ConnectError swallowed", exc_info=True)
    finally:
        ended = harness_client.end_trial(EndTrialRequest(trial_id=started.trial_id))

    score = float(ended.score)
    detail = list(ended.score_detail)
    probe_id = _persist_probe(
        db_path=db_path,
        task_id=task_id,
        instantiation_hash=instantiation_hash,
        probe_kind=probe_kind,
        ans=ans, refs=refs, writes=writes, outcome=outcome,
        score=score, score_detail=detail, trial_id=started.trial_id,
    )
    return probe_id, score, detail


def _build_probe(kind: str, known: dict[str, list[str]]) -> tuple[str, list[str], dict[str, str], int]:
    from bitgn.vm.pcm_pb2 import Outcome
    ans = (known.get("expected_answer") or [""])[0]
    refs = list(known.get("required_ref") or [])
    writes_paths = list(known.get("required_write") or [])
    writes = {p: f"local-probe content for {p}" for p in writes_paths}
    if kind == "P1_empty":
        return ("", [], {}, Outcome.OUTCOME_OK)
    if kind == "P2_extracted":
        return (ans, [], {}, Outcome.OUTCOME_OK)
    if kind == "P3_with_refs":
        return (ans, refs, {}, Outcome.OUTCOME_OK)
    if kind == "P4_with_writes":
        return (ans, refs, writes, Outcome.OUTCOME_OK)
    if kind == "P5_outcome_alt":
        return (ans, refs, writes, Outcome.OUTCOME_NONE_CLARIFICATION)
    raise ValueError(f"unknown probe kind: {kind}")


def _persist_probe(
    *,
    db_path: Path,
    task_id: str,
    instantiation_hash: str,
    probe_kind: str,
    ans: str,
    refs: list[str],
    writes: dict[str, str],
    outcome: int,
    score: float,
    score_detail: list[str],
    trial_id: str,
) -> int:
    """Insert into probe_log; return the new probe_id."""
    probed_at = datetime.now(tz=timezone.utc).isoformat()
    with sqlite3.connect(db_path) as cx:
        cx.execute("PRAGMA foreign_keys = ON")
        cur = cx.execute(
            """
            INSERT INTO probe_log
            (task_id, instantiation_hash, probe_kind, submitted_answer,
             submitted_refs, submitted_outcome, submitted_writes,
             score, score_detail_raw, trial_id, probed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, instantiation_hash, probe_kind, ans,
                json.dumps(refs), str(outcome), json.dumps(writes),
                score, json.dumps(score_detail), trial_id, probed_at,
            ),
        )
        cx.commit()
        return cur.lastrowid or 0


def _run_p2b(
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instantiation_hash: str,
    known: dict[str, list[str]],
    db_path: Path,
) -> int:
    """Single mutation probe — case-flip on the extracted answer."""
    from bitgn.vm.pcm_pb2 import Outcome

    base = (known.get("expected_answer") or [""])[0]
    if not base:
        return 0
    mutated = base.swapcase() if base != base.swapcase() else base + " "

    _run_single_probe(
        harness_client=harness_client,
        pcm_factory=pcm_factory,
        task_id=task_id,
        benchmark_id=benchmark_id,
        instantiation_hash=instantiation_hash,
        probe_kind="P2b_mutation",
        ans=mutated,
        refs=[],
        writes={},
        outcome=int(Outcome.OUTCOME_OK),
        db_path=db_path,
    )
    return 1


def _run_p6(
    harness_client: Any,
    pcm_factory: Callable[[str], Any],
    task_id: str,
    benchmark_id: str,
    instantiation_hash: str,
    db_path: Path,
) -> int:
    """Random-but-typed answer probe."""
    from bitgn.vm.pcm_pb2 import Outcome

    _run_single_probe(
        harness_client=harness_client,
        pcm_factory=pcm_factory,
        task_id=task_id,
        benchmark_id=benchmark_id,
        instantiation_hash=instantiation_hash,
        probe_kind="P6_random",
        ans="P6_RANDOM",
        refs=[],
        writes={},
        outcome=int(Outcome.OUTCOME_OK),
        db_path=db_path,
    )
    return 1
