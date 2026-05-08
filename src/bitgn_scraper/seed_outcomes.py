"""Mine existing PROD JSONL traces for failed-task score_detail strings.

Each agent run writes one .jsonl per task. We pick the kind=meta line
(for task_id, intent_head, benchmark) and the kind=outcome line (for
score and score_detail). Returns one OutcomeFinding per failed task
with non-empty detail.

These findings feed seed_rules.extract_rules to produce confidence='high'
rule rows without any new API calls — they are already-paid-for evidence
sitting in the repo from prior PROD runs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutcomeFinding:
    task_id: str
    intent_head: str
    benchmark_id: str
    score: float
    score_detail: list[str]
    source_path: str


def mine_outcomes_file(path: Path) -> list[OutcomeFinding]:
    """Parse one JSONL trace; return failed-task findings (may be 0)."""
    meta: dict | None = None
    outcome: dict | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("kind")
        if kind == "meta":
            meta = obj
        elif kind == "outcome":
            outcome = obj
    if meta is None or outcome is None:
        return []
    score = float(outcome.get("score", 0.0))
    detail = outcome.get("score_detail") or []
    if score >= 1.0:
        return []
    if not detail:
        return []
    return [OutcomeFinding(
        task_id=meta.get("task_id", ""),
        intent_head=meta.get("intent_head", ""),
        benchmark_id=meta.get("benchmark", ""),
        score=score,
        score_detail=list(detail),
        source_path=str(path),
    )]


def mine_outcomes_dir(root: Path) -> list[OutcomeFinding]:
    """Walk every *.jsonl file under root, aggregate findings."""
    out: list[OutcomeFinding] = []
    for p in sorted(root.rglob("*.jsonl")):
        out.extend(mine_outcomes_file(p))
    return out
