"""Trace writer — append-per-event JSONL with crash fallback."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitgn_contest_agent.trace_schema import (
    TRACE_SCHEMA_VERSION,
    TraceMeta,
    TraceOutcome,
    load_jsonl,
)
from bitgn_contest_agent.trace_writer import TraceWriter


def _mk_meta(task_id: str = "t1") -> TraceMeta:
    return TraceMeta(
        agent_version="0.0.7",
        agent_commit="dev",
        model="gpt-5.3-codex",
        backend="openai_compat",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        task_id=task_id,
        task_index=0,
        started_at="2026-04-10T00:00:00Z",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    )


def test_writer_appends_meta_and_flushes_each_record(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.append_task(task_id="t1", task_text="do stuff")
    w.append_prepass(
        cmd="tree", ok=True, bytes=10, wall_ms=5, error=None, error_code=None
    )
    w.close()

    records = list(load_jsonl(path))
    assert len(records) == 3
    assert records[0].kind == "meta"
    assert records[1].kind == "task"
    assert records[2].kind == "prepass"


def test_writer_survives_crash_and_writes_crashed_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.write_crash_sidecar("synthetic boom", traceback_text="tb here")
    # Sidecar path is next to the trace.
    sidecar = path.with_name(path.name.replace(".jsonl", "_CRASHED.json"))
    assert sidecar.exists()
    blob = json.loads(sidecar.read_text(encoding="utf-8"))
    assert blob["error"] == "synthetic boom"
    assert blob["traceback"] == "tb here"
    assert blob["partial_trace"] == str(path)


def test_patch_outcome_score_backfills_grader_verdict(tmp_path: Path) -> None:
    """T24 regression: the CLI knows the grader score only after the loop
    exits. patch_outcome_score must back-fill it into the already-written
    outcome so bench_summary sees the grader verdict, not the agent's
    self-reported OUTCOME_OK."""
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.append_task(task_id="t1", task_text="...")
    w.append_outcome(
        TraceOutcome(
            terminated_by="report_completion",
            reported="OUTCOME_OK",
            enforcer_bypassed=False,
            error_kind=None,
            error_msg=None,
            total_steps=3,
            total_llm_calls=3,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cached_tokens=0,
            score=None,
        )
    )
    w.close()

    w.patch_outcome_score(0.0)  # grader disagreed

    records = list(load_jsonl(path))
    assert len(records) == 3
    outcome = records[-1]
    assert isinstance(outcome, TraceOutcome)
    assert outcome.score == 0.0
    assert outcome.reported == "OUTCOME_OK"  # other fields untouched
    assert outcome.total_steps == 3


def test_patch_outcome_score_persists_grader_score_detail(tmp_path: Path) -> None:
    """Observability add (2026-04-11): the grader returns a list of
    human-readable strings alongside the score, explaining which checks
    failed. patch_outcome_score must persist them into the outcome record
    so content-layer failures (agent wrote something plausible but wrong)
    can be root-caused offline from the trace."""
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.append_task(task_id="t1", task_text="...")
    w.append_outcome(
        TraceOutcome(
            terminated_by="report_completion",
            reported="OUTCOME_OK",
            total_steps=4,
            total_llm_calls=4,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            score=None,
        )
    )
    w.close()

    w.patch_outcome_score(
        0.0,
        score_detail=[
            "check 'outbox/84446.json contains to=accounts-payable@...' FAILED",
            "expected recipient billing@helios-tax-group.biz",
        ],
    )

    records = list(load_jsonl(path))
    outcome = records[-1]
    assert isinstance(outcome, TraceOutcome)
    assert outcome.score == 0.0
    assert outcome.score_detail is not None
    assert len(outcome.score_detail) == 2
    assert "expected recipient" in outcome.score_detail[1]


def test_patch_outcome_score_omits_detail_when_none(tmp_path: Path) -> None:
    """Back-compat: calls that don't pass score_detail keep the field
    absent (or null) on the outcome record — existing bench summaries
    that ignore the field must continue to parse."""
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.append_outcome(
        TraceOutcome(
            terminated_by="report_completion",
            reported="OUTCOME_OK",
            total_steps=1,
            total_llm_calls=1,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            score=None,
        )
    )
    w.close()

    w.patch_outcome_score(1.0)  # no score_detail

    records = list(load_jsonl(path))
    outcome = records[-1]
    assert isinstance(outcome, TraceOutcome)
    assert outcome.score == 1.0
    assert outcome.score_detail is None


def test_patch_outcome_score_raises_if_writer_still_open(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.append_outcome(
        TraceOutcome(
            terminated_by="report_completion",
            reported="OUTCOME_OK",
            total_steps=1,
            total_llm_calls=1,
            total_prompt_tokens=0,
            total_completion_tokens=0,
        )
    )
    with pytest.raises(RuntimeError, match="after close"):
        w.patch_outcome_score(1.0)
    w.close()


def test_patch_outcome_score_raises_if_no_outcome_present(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())
    w.close()
    with pytest.raises(RuntimeError, match="no outcome record"):
        w.patch_outcome_score(1.0)


def test_writer_is_thread_safe_per_instance(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "trace.jsonl"
    w = TraceWriter(path=path)
    w.write_meta(_mk_meta())

    def worker(i: int) -> None:
        for _ in range(20):
            w.append_event(
                at_step=i, event_kind="rate_limit_backoff", wait_ms=10, attempt=1
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    w.close()

    records = list(load_jsonl(path))
    # 1 meta + 100 events
    assert len(records) == 101


def test_append_arch_writes_record(tmp_path) -> None:
    from bitgn_contest_agent.trace_writer import TraceWriter
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT1Rule
    )
    p = tmp_path / "t.jsonl"
    w = TraceWriter(path=p)
    w.append_arch(TraceArch(
        category=ArchCategory.VALIDATOR_T1,
        at_step=2,
        rule=ValidatorT1Rule.MUTATION_GUARD,
        details="tool=write",
    ))
    w.close()
    records = list(load_jsonl(p))
    assert len(records) == 1
    assert isinstance(records[0], TraceArch)
    assert records[0].rule == ValidatorT1Rule.MUTATION_GUARD


def test_append_prepass_accepts_schema_roots_and_match_fields(tmp_path):
    from bitgn_contest_agent.trace_writer import TraceWriter
    path = tmp_path / "t.jsonl"
    w = TraceWriter(path=path)
    w.append_prepass(
        cmd="preflight_schema",
        ok=True,
        bytes=100,
        wall_ms=5,
        schema_roots={
            "projects_root": "40_projects",
            "finance_roots": ["50_finance/invoices"],
            "entities_root": "20_entities",
            "inbox_root": "00_inbox",
            "outbox_root": "60_outbox/outbox",
        },
    )
    w.append_prepass(
        cmd="routed_preflight_project",
        ok=True,
        bytes=200,
        match_found=True,
        match_file="40_projects/studio_parts_library/README.MD",
    )
    w.close()
    lines = path.read_text().splitlines()
    import json
    r1 = json.loads(lines[0])
    assert r1["schema_roots"]["projects_root"] == "40_projects"
    r2 = json.loads(lines[1])
    assert r2["match_found"] is True
    assert r2["match_file"] == "40_projects/studio_parts_library/README.MD"
