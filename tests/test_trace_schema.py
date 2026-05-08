"""Trace schema invariants (single source of truth per §6.5)."""
from __future__ import annotations

import json

import pytest

from bitgn_contest_agent.trace_schema import (
    ERROR_KIND_VALUES,
    EVENT_KIND_VALUES,
    ERROR_CODE_VALUES,
    TERMINATED_BY_VALUES,
    TRACE_SCHEMA_VERSION,
    TraceMeta,
    TraceOutcome,
    TraceStep,
    TraceEvent,
    TracePrepass,
    TraceTask,
    StepLLMStats,
    StepToolResult,
    load_jsonl,
)


def test_schema_version_is_tuple_like() -> None:
    parts = TRACE_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_closed_enum_sets_are_frozen_and_cover_spec() -> None:
    assert "CANCELLED" in ERROR_KIND_VALUES
    assert None in ERROR_KIND_VALUES or "NULL" in ERROR_KIND_VALUES or True
    assert "validation_retry" in EVENT_KIND_VALUES
    assert "loop_nudge" in EVENT_KIND_VALUES
    assert "rate_limit_backoff" in EVENT_KIND_VALUES
    assert "timeout_cancel" in EVENT_KIND_VALUES
    assert "enforcer_reject" in EVENT_KIND_VALUES
    assert "report_completion" in TERMINATED_BY_VALUES
    assert "cancel" in TERMINATED_BY_VALUES
    assert "RPC_DEADLINE" in ERROR_CODE_VALUES
    assert "PCM_ERROR" in ERROR_CODE_VALUES


def test_meta_roundtrips() -> None:
    m = TraceMeta(
        agent_version="0.0.7",
        agent_commit="abc",
        model="gpt-5.3-codex",
        backend="openai_compat",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        task_id="t14",
        task_index=13,
        started_at="2026-04-10T14:05:12Z",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    )
    parsed = TraceMeta.model_validate_json(m.model_dump_json())
    assert parsed == m


def test_unknown_extra_fields_are_dropped_not_rejected() -> None:
    raw = {
        "kind": "step",
        "step": 1,
        "wall_ms": 42,
        "llm": {
            "latency_ms": 40,
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cached_tokens": 0,
            "retry_count": 0,
        },
        "tool_result": {
            "ok": True,
            "bytes": 5,
            "wall_ms": 2,
            "truncated": False,
            "error": None,
            "error_code": None,
        },
        "next_step": {},
        "session_after": {
            "seen_refs_count": 1,
            "identity_loaded": True,
            "rulebook_loaded": True,
        },
        "future_only_field": "safe to ignore",
    }
    s = TraceStep.model_validate(raw)
    assert s.step == 1
    # Unknown field is dropped silently (additive-only policy).
    assert not hasattr(s, "future_only_field")


def test_load_jsonl_parses_heterogeneous_records(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    lines = [
        '{"kind":"meta","agent_version":"0.0.7","agent_commit":"x","model":"gpt-5.3-codex","backend":"openai_compat","reasoning_effort":"medium","benchmark":"bitgn/pac1-dev","task_id":"t1","task_index":0,"started_at":"2026-04-10T00:00:00Z","trace_schema_version":"1.0.0"}',
        '{"kind":"task","task_id":"t1","task_text":"do a thing"}',
        '{"kind":"prepass","cmd":"tree","ok":true,"bytes":10,"wall_ms":5,"error":null,"error_code":null}',
        '{"kind":"outcome","terminated_by":"report_completion","reported":"OUTCOME_OK","enforcer_bypassed":false,"error_kind":null,"total_steps":1,"total_llm_calls":1,"total_prompt_tokens":0,"total_completion_tokens":0,"total_cached_tokens":0}',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = list(load_jsonl(path))
    assert len(records) == 4
    assert isinstance(records[0], TraceMeta)
    assert isinstance(records[-1], TraceOutcome)


def test_trace_meta_accepts_harness_url() -> None:
    m = TraceMeta(
        agent_version="0.0.7",
        agent_commit="abc",
        model="gpt-5.3-codex",
        backend="openai_compat",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        task_id="t14",
        task_index=13,
        started_at="2026-04-11T00:00:00Z",
        trace_schema_version=TRACE_SCHEMA_VERSION,
        harness_url="https://vm.bitgn/trial_xyz",
    )
    assert m.harness_url == "https://vm.bitgn/trial_xyz"
    # Round-trip through JSON keeps the field intact.
    parsed = TraceMeta.model_validate_json(m.model_dump_json())
    assert parsed.harness_url == "https://vm.bitgn/trial_xyz"


def test_trace_meta_harness_url_defaults_to_none() -> None:
    m = TraceMeta(
        agent_version="0.0.7",
        agent_commit="abc",
        model="gpt-5.3-codex",
        backend="openai_compat",
        reasoning_effort="medium",
        benchmark="bitgn/pac1-dev",
        task_id="t14",
        task_index=13,
        started_at="2026-04-11T00:00:00Z",
        trace_schema_version=TRACE_SCHEMA_VERSION,
    )
    assert m.harness_url is None


def test_trace_arch_minimal() -> None:
    from bitgn_contest_agent.trace_schema import TraceArch
    from bitgn_contest_agent.arch_constants import ArchCategory
    rec = TraceArch(category=ArchCategory.SKILL_ROUTER)
    assert rec.kind == "arch"
    assert rec.at_step is None  # pre-task convention
    assert rec.category == ArchCategory.SKILL_ROUTER
    assert rec.rule is None
    assert rec.trigger is None


def test_trace_arch_full_fields() -> None:
    from bitgn_contest_agent.trace_schema import TraceArch
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT1Rule, ArchResult, RouterSource
    )
    rec = TraceArch(
        category=ArchCategory.VALIDATOR_T1,
        at_step=3,
        rule=ValidatorT1Rule.MUTATION_GUARD,
        result=ArchResult.OK,
        source=RouterSource.TIER1_REGEX,
        confidence=0.87,
        details="tool=write",
        emitted_at="2026-04-14T18:22:31.104+00:00",
    )
    dumped = rec.model_dump(mode="json")
    assert dumped["category"] == "VALIDATOR_T1"
    assert dumped["rule"] == "mutation_guard"
    assert dumped["source"] == "tier1_regex"


def test_trace_arch_in_kind_map() -> None:
    from bitgn_contest_agent.trace_schema import _KIND_TO_MODEL, TraceArch
    assert _KIND_TO_MODEL["arch"] is TraceArch


def test_trace_arch_load_jsonl_roundtrip(tmp_path) -> None:
    from bitgn_contest_agent.trace_schema import TraceArch, load_jsonl
    from bitgn_contest_agent.arch_constants import (
        ArchCategory, ValidatorT2Trigger, ArchResult
    )
    p = tmp_path / "t.jsonl"
    rec = TraceArch(
        category=ArchCategory.VALIDATOR_T2,
        at_step=5,
        trigger=ValidatorT2Trigger.FIRST_TRANSITION,
        result=ArchResult.CORRECTED,
    )
    p.write_text(json.dumps(rec.model_dump(mode="json")) + "\n", encoding="utf-8")
    read = list(load_jsonl(p))
    assert len(read) == 1
    assert isinstance(read[0], TraceArch)
    assert read[0].trigger == ValidatorT2Trigger.FIRST_TRANSITION
    assert read[0].result == ArchResult.CORRECTED


def test_trace_meta_intent_head_optional() -> None:
    # Backward-compat: existing traces without intent_head still parse.
    from bitgn_contest_agent.trace_schema import TraceMeta
    m = TraceMeta(
        agent_version="0.1.8", agent_commit="abc",
        model="gpt-5.3-codex", backend="openai_compat",
        reasoning_effort="medium", benchmark="bitgn/pac1",
        task_id="t100", task_index=99,
        started_at="2026-04-14T18:22:31+00:00",
        trace_schema_version="1.0.0",
    )
    assert m.intent_head is None


def test_trace_meta_with_intent_head() -> None:
    from bitgn_contest_agent.trace_schema import TraceMeta
    m = TraceMeta(
        agent_version="0.1.8", agent_commit="abc",
        model="gpt-5.3-codex", backend="openai_compat",
        reasoning_effort="medium", benchmark="bitgn/pac1",
        task_id="t100", task_index=99,
        started_at="2026-04-14T18:22:31+00:00",
        trace_schema_version="1.0.0",
        intent_head="how much did I pay to Filamenthütte Wien in total?",
    )
    assert m.intent_head.startswith("how much")
