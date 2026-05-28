"""Unit tests for judge_enforcer — mocked LLM, exercises the
parse path, hard-rule guards, and confidence fallback."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from bitgn_contest_agent.judge_enforcer import (
    JudgeApplyResult,
    JudgeInput,
    JudgeVerdict,
    _strip_fences,
    apply,
    judge,
)


def _input(**overrides) -> JudgeInput:
    base = dict(
        task_text="A support note claims we stock the Wrench from Acme.",
        task_text_en="A support note claims we stock the Wrench from Acme.",
        task_spec_kind="yes_no_sku",
        outcome="OUTCOME_OK",
        message="<YES> we stock WRC-12345 from Acme.",
        cited_refs=["/proc/catalog/Acme/WRC-12345.json"],
        seen_refs=frozenset({
            "/AGENTS.MD",
            "/proc/catalog/Acme/WRC-12345.json",
            "/proc/catalog/Acme/WRC-67890.json",
            "/docs/security.md",
        }),
        actor_id="emp_001",
        actor_roles="employee",
    )
    base.update(overrides)
    return JudgeInput(**base)


def _patch_classifier(response: str):
    """Patch classifier.raw_completion to return a fixed body."""
    from bitgn_contest_agent import classifier as _cm
    return patch.object(_cm, "raw_completion", side_effect=lambda **k: response)


# ── _strip_fences ─────────────────────────────────────────────────────


class TestStripFences:
    def test_plain_json_unchanged(self):
        assert _strip_fences('{"a":1}') == '{"a":1}'

    def test_fenced_json_stripped(self):
        assert _strip_fences('```json\n{"a":1}\n```') == '{"a":1}'

    def test_fenced_no_lang_stripped(self):
        assert _strip_fences('```\n{"a":1}\n```') == '{"a":1}'

    def test_whitespace_stripped(self):
        assert _strip_fences('   {"a":1}  ') == '{"a":1}'


# ── judge() — transport + parse ───────────────────────────────────────


class TestJudgeCall:
    def test_empty_input_returns_none(self):
        inp = _input(task_text="", cited_refs=[])
        assert judge(inp) is None

    def test_well_formed_response_parses(self):
        resp = json.dumps({
            "keep_refs": ["/proc/catalog/Acme/WRC-12345.json"],
            "add_refs": [],
            "drop_refs": [],
            "reasons": ["all refs valid"],
            "confidence": 0.9,
        })
        with _patch_classifier(resp):
            v = judge(_input())
        assert v is not None
        assert v.confidence == 0.9
        assert v.keep_refs == ["/proc/catalog/Acme/WRC-12345.json"]

    def test_fenced_response_parses(self):
        body = json.dumps({
            "keep_refs": [],
            "add_refs": [],
            "drop_refs": ["/proc/catalog/Acme/WRC-12345.json"],
            "reasons": ["wrong attribute"],
            "confidence": 0.8,
        })
        with _patch_classifier(f"```json\n{body}\n```"):
            v = judge(_input())
        assert v is not None
        assert v.drop_refs == ["/proc/catalog/Acme/WRC-12345.json"]

    def test_garbage_response_returns_none(self):
        with _patch_classifier("not json"):
            v = judge(_input())
        assert v is None

    def test_classifier_exception_returns_none(self):
        from bitgn_contest_agent import classifier as _cm
        with patch.object(_cm, "raw_completion",
                          side_effect=RuntimeError("boom")):
            v = judge(_input())
        assert v is None

    def test_empty_response_returns_none(self):
        with _patch_classifier(""):
            v = judge(_input())
        assert v is None


# ── apply() — hard-rule guards ────────────────────────────────────────


class TestApplyHardGuards:
    def test_low_confidence_does_not_apply(self):
        inp = _input()
        v = JudgeVerdict(
            keep_refs=[],
            add_refs=["/proc/catalog/Acme/WRC-67890.json"],
            drop_refs=["/proc/catalog/Acme/WRC-12345.json"],
            reasons=["test"],
            confidence=0.4,
        )
        result = apply(input_=inp, verdict=v)
        assert not result.applied
        # Falls back to original refs
        assert result.final_refs == list(inp.cited_refs)

    def test_high_confidence_applies(self):
        inp = _input()
        v = JudgeVerdict(
            keep_refs=["/proc/catalog/Acme/WRC-12345.json"],
            add_refs=["/proc/catalog/Acme/WRC-67890.json"],
            drop_refs=[],
            reasons=["add the family sibling"],
            confidence=0.85,
        )
        result = apply(input_=inp, verdict=v)
        assert result.applied
        assert "/proc/catalog/Acme/WRC-67890.json" in result.final_refs
        assert "/proc/catalog/Acme/WRC-12345.json" in result.final_refs

    def test_add_must_be_in_seen_refs(self):
        """RULE 1 — hallucinated path is rejected even if confident."""
        inp = _input()
        v = JudgeVerdict(
            keep_refs=["/proc/catalog/Acme/WRC-12345.json"],
            add_refs=["/proc/catalog/Acme/HALLUCINATED.json"],
            drop_refs=[],
            confidence=0.95,
        )
        result = apply(input_=inp, verdict=v)
        assert result.applied
        # Hallucinated ref filtered out
        assert "/proc/catalog/Acme/HALLUCINATED.json" not in result.final_refs

    def test_drop_only_affects_cited_refs(self):
        """drop_refs that name paths the agent didn't cite are no-op."""
        inp = _input()
        v = JudgeVerdict(
            keep_refs=[],
            drop_refs=["/proc/catalog/Acme/NOTCITED.json"],
            confidence=0.9,
        )
        result = apply(input_=inp, verdict=v)
        # Original cited refs survive
        assert "/proc/catalog/Acme/WRC-12345.json" in result.final_refs

    def test_drop_removes_cited(self):
        inp = _input(cited_refs=[
            "/proc/catalog/Acme/WRC-12345.json",
            "/proc/catalog/UNRELATED/XX.json",
        ], seen_refs=frozenset({
            "/proc/catalog/Acme/WRC-12345.json",
            "/proc/catalog/UNRELATED/XX.json",
        }))
        v = JudgeVerdict(
            drop_refs=["/proc/catalog/UNRELATED/XX.json"],
            reasons=["wrong family"],
            confidence=0.9,
        )
        result = apply(input_=inp, verdict=v)
        assert result.applied
        assert "/proc/catalog/Acme/WRC-12345.json" in result.final_refs
        assert "/proc/catalog/UNRELATED/XX.json" not in result.final_refs
        assert result.dropped == ["/proc/catalog/UNRELATED/XX.json"]

    def test_confidence_floor_param_respected(self):
        inp = _input()
        v = JudgeVerdict(
            drop_refs=["/proc/catalog/Acme/WRC-12345.json"],
            confidence=0.65,
        )
        # Default floor 0.5 → applies
        r1 = apply(input_=inp, verdict=v)
        assert r1.applied
        # Stricter floor 0.7 → does not apply
        r2 = apply(input_=inp, verdict=v, confidence_floor=0.7)
        assert not r2.applied
        assert r2.final_refs == list(inp.cited_refs)


# ── pydantic schema sanity ────────────────────────────────────────────


class TestVerdictSchema:
    def test_confidence_clamped(self):
        with pytest.raises(ValidationError):
            JudgeVerdict(confidence=1.5)
        with pytest.raises(ValidationError):
            JudgeVerdict(confidence=-0.1)

    def test_default_empty_lists(self):
        v = JudgeVerdict()
        assert v.keep_refs == []
        assert v.add_refs == []
        assert v.drop_refs == []
        assert v.reasons == []
        assert v.confidence == 0.0
